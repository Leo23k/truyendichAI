"""Bounded, serverless discovery-only polling of novel source indexes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from app.crawlers.bootstrap import registry
from app.notification_service import deliver_pending_push, notify_chapter_discovery
from sync.service import SupabaseSync, enqueue_remote_crawl

logger = logging.getLogger(__name__)


def _identity(url: str) -> str:
    return "url:" + url.strip().rstrip("/")


def run_chapter_monitor(batch_size: int = 4) -> dict[str, int]:
    remote = SupabaseSync()
    novels = remote.all_rows("novels", {
        "select": "id,slug,title,source_url,source_type,deleted_at",
        "deleted_at": "is.null", "source_url": "not.is.null",
    })
    novels_by_id = {int(row["id"]): row for row in novels if row.get("id")}
    all_states = remote.all_rows("chapter_monitor_state", {
        "select": "novel_id,last_checked_at", "order": "last_checked_at.asc.nullsfirst",
    })
    all_state_ids = {int(row["novel_id"]) for row in all_states}
    states_added = False
    for novel_id, novel in novels_by_id.items():
        if novel_id in all_state_ids:
            continue
        # Newly added novels start from a baseline instead of announcing their
        # entire existing source index as if it had just been published.
        known_chapters = remote.all_rows("chapters", {
            "select": "chapter_number,title,source_url,content_path", "novel_id": f"eq.{novel_id}",
        })
        if known_chapters:
            remote.insert_many_ignore("chapter_discoveries", [
                {
                    "novel_id": novel_id,
                    "source_identity": _identity(str(chapter["source_url"])),
                    "chapter_number": max(1, int(chapter.get("chapter_number") or 1)),
                    "chapter_title": str(chapter.get("title") or "Chapter")[:500],
                    "source_url": chapter["source_url"],
                    "crawled_at": datetime.now(timezone.utc).isoformat() if chapter.get("content_path") else None,
                }
                for chapter in known_chapters if chapter.get("source_url")
            ], "novel_id,source_identity")
        latest = max((int(row.get("chapter_number") or 0) for row in known_chapters), default=0)
        latest_url = next((row.get("source_url") for row in reversed(known_chapters) if row.get("source_url")), None)
        remote.upsert("chapter_monitor_state", {
            "novel_id": novel_id,
            "last_seen_source_url": latest_url,
            "last_seen_chapter_number": latest,
        }, "novel_id")
        states_added = True
    if states_added:
        all_states = remote.all_rows("chapter_monitor_state", {
            "select": "novel_id,last_checked_at", "order": "last_checked_at.asc.nullsfirst",
        })
    states = [row for row in all_states if int(row["novel_id"]) in novels_by_id][
        :max(1, min(12, batch_size))
    ]
    counts = {"checked": 0, "discovered": 0, "errors": 0, "push_sent": 0}
    for state in states:
        novel = novels_by_id.get(int(state["novel_id"]))
        if not novel:
            continue
        novel_id = int(novel["id"])
        now = datetime.now(timezone.utc).isoformat()
        counts["checked"] += 1
        try:
            crawler = registry.get_for_url(str(novel["source_url"]))
            chapters = asyncio.run(asyncio.wait_for(crawler.get_latest_chapters(novel["source_url"], 40), timeout=18))
            existing = remote.all_rows("chapter_discoveries", {
                "select": "source_identity", "novel_id": f"eq.{novel_id}",
            })
            known = {str(row.get("source_identity")) for row in existing}
            if not known and state.get("last_checked_at") is None:
                baseline = [{
                    "novel_id": novel_id,
                    "source_identity": _identity(chapter.url),
                    "chapter_number": max(1, int(chapter.chapter_number)),
                    "chapter_title": str(chapter.title or f"Chương {chapter.chapter_number}")[:500],
                    "source_url": chapter.url,
                } for chapter in chapters if chapter.url]
                if baseline:
                    remote.insert_many_ignore("chapter_discoveries", baseline, "novel_id,source_identity")
                    enqueue_remote_crawl(str(novel["source_url"]), str(novel.get("source_type") or "zh"),
                        {"chapter_monitor": True, "novel_id": novel_id})
                remote.patch_rows("chapter_monitor_state", {"novel_id": f"eq.{novel_id}"}, {
                    "last_seen_chapter_number": max((int(ch.chapter_number) for ch in chapters), default=0),
                    "last_seen_source_url": chapters[-1].url if chapters else None,
                    "last_checked_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                continue
            discoveries = []
            for chapter in chapters:
                identity = _identity(chapter.url)
                if identity in known or not chapter.url:
                    continue
                discoveries.append({
                    "novel_id": novel_id,
                    "source_identity": identity,
                    "chapter_number": max(1, int(chapter.chapter_number)),
                    "chapter_title": str(chapter.title or f"Chương {chapter.chapter_number}")[:500],
                    "source_url": chapter.url,
                })
            if discoveries:
                for discovery in discoveries:
                    discovery["notification_after_translation"] = True
                # Insert-ignore makes concurrent Cron invocations safe. Return
                # inserted rows only, so notifications exist only for new events.
                response = remote.request(
                    "POST", "/rest/v1/chapter_discoveries?on_conflict=novel_id,source_identity",
                    json=discoveries,
                    headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
                )
                inserted = response.json()
                if not isinstance(inserted, list):
                    inserted = []
                chapter_rows = remote.all_rows("chapters", {
                    "select": "source_url,title,translated", "novel_id": f"eq.{novel_id}",
                })
                chapters_by_source = {str(row.get("source_url")): row for row in chapter_rows if row.get("source_url")}
                for discovery in inserted:
                    counts["discovered"] += 1
                    notify_chapter_discovery(remote, discovery, novel, crawler_only=True)
                    crawled_chapter = chapters_by_source.get(str(discovery.get("source_url") or ""))
                    if crawled_chapter and crawled_chapter.get("translated") and crawled_chapter.get("title"):
                        notify_chapter_discovery(
                            remote,
                            {**discovery, "chapter_title": crawled_chapter["title"]},
                            novel,
                            update_existing=True,
                        )
                        remote.patch_rows(
                            "chapter_discoveries", {"id": f"eq.{discovery['id']}"},
                            {"notification_after_translation": False},
                        )
                if inserted:
                    enqueue_remote_crawl(
                        str(novel["source_url"]), str(novel.get("source_type") or "zh"),
                        {"chapter_monitor": True, "novel_id": novel_id},
                    )
            latest = max((int(ch.chapter_number) for ch in chapters), default=0)
            remote.patch_rows("chapter_monitor_state", {"novel_id": f"eq.{novel_id}"}, {
                "last_seen_chapter_number": latest,
                "last_seen_source_url": chapters[-1].url if chapters else None,
                "last_checked_at": now,
                "last_error": None,
                "updated_at": now,
            })
        except Exception as exc:
            counts["errors"] += 1
            remote.patch_rows("chapter_monitor_state", {"novel_id": f"eq.{novel_id}"}, {
                "last_checked_at": now,
                "last_error": str(exc)[:1000],
                "updated_at": now,
            })
            logger.warning("Chapter monitor failed novel_id=%s source=%s error=%s", novel_id, novel.get("source_url"), exc)
    try:
        counts["push_sent"] = deliver_pending_push(20)
    except Exception:
        logger.exception("Pending Web Push delivery batch failed")
    return counts
