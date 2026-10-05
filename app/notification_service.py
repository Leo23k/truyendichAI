"""Server-side follow, notification, settings, and Web Push persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

from sync.service import SupabaseSync


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
    return [row for row in rows if isinstance(row, dict)], total


def unread_notification_count(user_id: str) -> int:
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
    if parsed.scheme != "https" or not parsed.hostname:
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
