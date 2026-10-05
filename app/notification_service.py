"""Server-side follow, notification, settings, and Web Push persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from urllib.parse import urlparse
from uuid import UUID

from sync.service import SupabaseSync

logger = logging.getLogger(__name__)


DEFAULT_NOTIFICATION_SETTINGS = {
    "follow_notifications": True,
    "crawler_notifications": True,
    "web_notifications": True,
    "push_notifications": False,
}


class NotificationDataError(RuntimeError):
    """Raised when notification data cannot be read or written safely."""


def follow_novel(user_id: str, novel_id: int) -> None:
    remote = SupabaseSync()
    if not remote.rows(
        "novels",
        {"select": "id", "id": f"eq.{novel_id}", "deleted_at": "is.null", "limit": "1"},
    ):
        raise LookupError("Novel was not found")
    remote.request(
        "POST",
        "/rest/v1/novel_followers?on_conflict=user_id,novel_id",
        json={"user_id": user_id, "novel_id": novel_id},
        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
    )


def unfollow_novel(user_id: str, novel_id: int) -> None:
    SupabaseSync().delete_rows(
        "novel_followers", {"user_id": f"eq.{user_id}", "novel_id": f"eq.{novel_id}"}
    )


def is_following_novel(user_id: str, novel_id: int) -> bool:
    return bool(
        SupabaseSync().rows(
            "novel_followers",
            {
                "select": "novel_id",
                "user_id": f"eq.{user_id}",
                "novel_id": f"eq.{novel_id}",
                "limit": "1",
            },
        )
    )


def list_followed_novels(
    user_id: str, *, query: str = "", filter_by: str = "all", page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    page = max(1, page)
    page_size = max(1, min(50, page_size))
    rows = SupabaseSync().rpc_rows(
        "list_followed_novels",
        {
            "p_user_id": user_id,
            "p_query": " ".join(query.split()),
            "p_filter": filter_by if filter_by in {"new", "read"} else "all",
            "p_limit": page_size,
            "p_offset": (page - 1) * page_size,
        },
    )
    total = int(rows[0].get("total_count") or 0) if rows else 0
    return rows, total


def list_notifications(user_id: str, *, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    if not get_notification_settings(user_id).get("web_notifications", True):
        return [], 0
    remote = SupabaseSync()
    page = max(1, page)
    page_size = max(1, min(50, page_size))
    response = remote.request(
        "GET",
        "/rest/v1/notifications",
        params={
            "select": "id,type,title,message,novel_id,discovery_id,chapter_number,url,is_read,created_at",
            "user_id": f"eq.{user_id}",
            "order": "created_at.desc,id.desc",
            "limit": str(page_size),
            "offset": str((page - 1) * page_size),
        },
        headers={"Prefer": "count=exact"},
    )
    rows = response.json()
    if not isinstance(rows, list):
        raise NotificationDataError("Supabase did not return notification rows")
    content_range = response.headers.get("content-range", "")
    try:
        total = int(content_range.rsplit("/", 1)[1])
    except (IndexError, ValueError):
        total = len(rows)
    notifications = [row for row in rows if isinstance(row, dict)]
    for row in notifications:
        row["created_at_display"] = format_notification_time(row.get("created_at"))
    return notifications, total


def format_notification_time(value: object) -> str:
    """Format notification timestamps in Vietnam local time."""
    try:
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    vietnam_time = timezone(timedelta(hours=7))
    return timestamp.astimezone(vietnam_time).strftime("%H:%M - %d/%m/%Y")


def unread_notification_count(user_id: str) -> int:
    if not get_notification_settings(user_id).get("web_notifications", True):
        return 0
    response = SupabaseSync().request(
        "GET",
        "/rest/v1/notifications",
        params={"select": "id", "user_id": f"eq.{user_id}", "is_read": "eq.false", "limit": "1"},
        headers={"Prefer": "count=exact"},
    )
    content_range = response.headers.get("content-range", "")
    try:
        return int(content_range.rsplit("/", 1)[1])
    except (IndexError, ValueError) as exc:
        raise NotificationDataError("Supabase did not return an unread count") from exc


def mark_notification_read(user_id: str, notification_id: str) -> bool:
    try:
        notification_id = str(UUID(notification_id))
    except (ValueError, TypeError, AttributeError):
        return False
    rows = SupabaseSync().patch_rows(
        "notifications",
        {"id": f"eq.{notification_id}", "user_id": f"eq.{user_id}", "is_read": "eq.false"},
        {"is_read": True},
    )
    return bool(rows)


def mark_all_notifications_read(user_id: str) -> int:
    rows = SupabaseSync().patch_rows(
        "notifications",
        {"user_id": f"eq.{user_id}", "is_read": "eq.false"},
        {"is_read": True},
    )
    return len(rows)


def get_notification_settings(user_id: str) -> dict:
    rows = SupabaseSync().rows(
        "notification_settings",
        {"select": "follow_notifications,crawler_notifications,web_notifications,push_notifications", "user_id": f"eq.{user_id}", "limit": "1"},
    )
    return {**DEFAULT_NOTIFICATION_SETTINGS, **(rows[0] if rows else {})}


def update_notification_settings(user_id: str, values: dict[str, bool]) -> dict:
    changes = {key: bool(value) for key, value in values.items() if key in DEFAULT_NOTIFICATION_SETTINGS}
    payload = {
        **get_notification_settings(user_id),
        **changes,
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return SupabaseSync().upsert("notification_settings", payload, "user_id")


def save_push_subscription(
    user_id: str, *, endpoint: str, p256dh: str, auth: str, user_agent: str | None
) -> None:
    parsed = urlparse(endpoint)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    allowed_push_hosts = {
        "fcm.googleapis.com", "updates.push.services.mozilla.com",
        "push.services.mozilla.com", "web.push.apple.com",
    }
    valid_host = hostname in allowed_push_hosts or hostname.endswith(".notify.windows.com")
    if parsed.scheme != "https" or not hostname or not valid_host or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("Push endpoint must use HTTPS")
    if not endpoint or not p256dh or not auth:
        raise ValueError("Push subscription is incomplete")
    remote = SupabaseSync()
    existing = remote.rows(
        "push_subscriptions",
        {"select": "id,user_id", "endpoint": f"eq.{endpoint}", "limit": "1"},
    )
    payload = {
        "p256dh": p256dh,
        "auth": auth,
        "user_agent": (user_agent or "")[:512] or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        if existing[0].get("user_id") != user_id:
            raise ValueError("Push subscription already belongs to another account")
        remote.patch_rows(
            "push_subscriptions",
            {"id": f"eq.{existing[0]['id']}", "user_id": f"eq.{user_id}"},
            payload,
        )
        return
    try:
        remote.request(
            "POST",
            "/rest/v1/push_subscriptions",
            json={
                **payload,
            "user_id": user_id,
            "endpoint": endpoint,
            },
            headers={"Prefer": "return=minimal"},
        )
    except Exception:
        # Concurrent subscribe requests are harmless only when this same user
        # won the insert race; never transfer an endpoint across accounts.
        raced = remote.rows(
            "push_subscriptions",
            {"select": "id,user_id", "endpoint": f"eq.{endpoint}", "limit": "1"},
        )
        if not raced or raced[0].get("user_id") != user_id:
            raise
        remote.patch_rows(
            "push_subscriptions",
            {"id": f"eq.{raced[0]['id']}", "user_id": f"eq.{user_id}"},
            payload,
        )


def delete_push_subscription(user_id: str, endpoint: str) -> None:
    SupabaseSync().delete_rows(
        "push_subscriptions", {"user_id": f"eq.{user_id}", "endpoint": f"eq.{endpoint}"}
    )


def notify_chapter_discovery(
    remote: SupabaseSync,
    discovery: dict,
    novel: dict,
    *,
    update_existing: bool = False,
    crawler_only: bool = False,
) -> int:
    """Create a source alert for admins or a translated chapter alert for followers."""
    novel_id = int(discovery["novel_id"])
    discovery_id = str(discovery["id"])
    followers = remote.rows("novel_followers", {
        "select": "user_id", "novel_id": f"eq.{novel_id}",
    })
    admins = remote.rows("profiles", {"select": "id", "role": "eq.admin"})
    follower_ids = {str(row["user_id"]) for row in followers if row.get("user_id")}
    admin_ids = {str(row["id"]) for row in admins if row.get("id")}
    user_ids = admin_ids if crawler_only else follower_ids | admin_ids
    if not user_ids:
        return 0
    settings_rows = remote.rows("notification_settings", {
        "select": "user_id,follow_notifications,crawler_notifications,web_notifications,push_notifications",
        "user_id": f"in.({','.join(user_ids)})",
    })
    settings_by_user = {str(row["user_id"]): row for row in settings_rows}
    total = 0
    title = f"{novel.get('title') or 'Truyện'} có chương mới"
    message = str(discovery.get("chapter_title") or f"Chương {discovery.get('chapter_number')}")
    url = f"/truyen/{novel.get('slug')}"
    for user_id in user_ids:
        follows = user_id in follower_ids and not crawler_only
        is_admin = user_id in admin_ids
        kind = "NEW_CHAPTER" if follows else "CRAWLER_NEW_CHAPTER"
        preferences = settings_by_user.get(user_id, {})
        enabled = (
            is_admin and preferences.get("crawler_notifications", True)
            if crawler_only or not follows
            else preferences.get("follow_notifications", True)
        )
        if not enabled:
            continue
        if not follows and not is_admin:
            continue
        payload = {
            "user_id": user_id,
            "event_key": f"chapter:{discovery_id}:user:{user_id}",
            "type": kind,
            "title": title,
            "message": message,
            "novel_id": novel_id,
            "discovery_id": discovery_id,
            "chapter_number": discovery.get("chapter_number"),
            "url": url,
            "is_read": False,
        }
        response = remote.request(
            "POST", "/rest/v1/notifications?on_conflict=event_key", json=payload,
            headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
        )
        created = response.json()
        if not isinstance(created, list) or not created:
            if update_existing:
                remote.patch_rows(
                    "notifications",
                    {"event_key": f"eq.{payload['event_key']}", "user_id": f"eq.{user_id}"},
                    {"message": message},
                )
            continue
        notification_id = created[0]["id"]
        total += 1
        if preferences.get("push_notifications", False):
            subscriptions = remote.rows("push_subscriptions", {
                "select": "id", "user_id": f"eq.{user_id}",
            })
            if subscriptions:
                remote.insert_many_ignore("push_deliveries", [
                    {"notification_id": notification_id, "subscription_id": item["id"]}
                    for item in subscriptions
                ], "notification_id,subscription_id")
    return total


def deliver_pending_push(limit: int = 20) -> int:
    private_key = os.getenv("VAPID_PRIVATE_KEY", "")
    subject = os.getenv("VAPID_SUBJECT", "")
    if not private_key or not subject:
        return 0
    from pywebpush import WebPushException, webpush

    remote = SupabaseSync()
    pending = remote.rows("push_deliveries", {
        "select": "id,attempts,notification_id,subscription_id",
        "sent_at": "is.null", "attempts": "lt.5", "order": "created_at.asc", "limit": str(max(1, min(limit, 30))),
    })
    delivered = 0
    for delivery in pending:
        subscriptions = remote.rows("push_subscriptions", {
            "select": "id,user_id,endpoint,p256dh,auth", "id": f"eq.{delivery['subscription_id']}", "limit": "1",
        })
        notifications = remote.rows("notifications", {
            "select": "title,message,url,chapter_number,novel_id", "id": f"eq.{delivery['notification_id']}", "limit": "1",
        })
        if not subscriptions or not notifications:
            continue
        subscription = subscriptions[0]
        notification = notifications[0]
        if not get_notification_settings(str(subscription["user_id"])).get("push_notifications", False):
            remote.patch_rows("push_deliveries", {"id": f"eq.{delivery['id']}"}, {
                "attempts": 5, "last_error": "Push disabled by user",
            })
            continue
        try:
            push_host = (urlparse(str(subscription["endpoint"])).hostname or "").lower()
            push_headers = {"X-WNS-Type": "wns/toast"} if push_host.endswith(".notify.windows.com") else None
            webpush(
                subscription_info={"endpoint": subscription["endpoint"], "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
                data=json.dumps({**notification, "id": delivery["notification_id"]}),
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=86400,
                headers=push_headers,
            )
            remote.patch_rows("push_deliveries", {"id": f"eq.{delivery['id']}"}, {
                "sent_at": datetime.now(timezone.utc).isoformat(), "last_error": None,
            })
            delivered += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in {404, 410}:
                delete_push_subscription(str(subscription["user_id"]), str(subscription["endpoint"]))
                remote.patch_rows("push_deliveries", {"id": f"eq.{delivery['id']}"}, {
                    "attempts": 5, "last_error": "Push subscription expired",
                })
                continue
            attempts = int(delivery.get("attempts") or 0) + 1
            remote.patch_rows("push_deliveries", {"id": f"eq.{delivery['id']}"}, {
                "attempts": attempts, "last_error": str(exc)[:500],
            })
            logger.warning("Web Push failed status=%s attempt=%s", status, attempts)
    return delivered
