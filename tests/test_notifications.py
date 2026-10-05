from __future__ import annotations

from types import SimpleNamespace

from app.notification_service import format_notification_time, notify_chapter_discovery
from sync.service import record_crawled_chapter_discovery


class FakeSupabase:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}

    def rows(self, table: str, params: dict[str, str]) -> list[dict]:
        if table == "novel_followers":
            return [{"user_id": "admin-following"}, {"user_id": "reader"}]
        if table == "profiles":
            return [{"id": "admin-following"}, {"id": "admin-not-following"}]
        if table == "notification_settings":
            return []
        raise AssertionError(f"Unexpected table: {table}")

    def request(self, method: str, path: str, *, json: dict, headers: dict) -> SimpleNamespace:
        assert method == "POST"
        event_key = json["event_key"]
        if event_key in self.events:
            return SimpleNamespace(json=lambda: [])
        self.events[event_key] = json
        return SimpleNamespace(json=lambda: [{"id": f"id-{len(self.events)}"}])

    def insert_many_ignore(self, *args, **kwargs) -> None:
        raise AssertionError("Push is disabled by default")

    def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
        assert table == "notifications"
        key = params["event_key"].removeprefix("eq.")
        user_id = params["user_id"].removeprefix("eq.")
        for event in self.events.values():
            if event["event_key"] == key and event["user_id"] == user_id:
                event.update(payload)
                return [event]
        return []


def test_notification_time_uses_vietnam_timezone_and_requested_format() -> None:
    assert format_notification_time("2026-10-05T11:08:45.759021+00:00") == "18:08 - 05/10/2026"
    assert format_notification_time("2026-10-05T01:00:00Z") == "08:00 - 05/10/2026"
    assert format_notification_time("not-a-timestamp") == ""


def test_discovery_creates_one_notification_per_user_and_deduplicates_admin_follow() -> None:
    remote = FakeSupabase()
    discovery = {
        "id": "discovery-1", "novel_id": 15, "chapter_number": 2315,
        "chapter_title": "Chapter 2315", "source_url": "https://source.test/chapter/2315",
    }
    novel = {"slug": "novel-15", "title": "Novel 15"}

    assert notify_chapter_discovery(remote, discovery, novel) == 3
    assert notify_chapter_discovery(remote, discovery, novel) == 0
    assert len(remote.events) == 3
    types_by_user = {event["user_id"]: event["type"] for event in remote.events.values()}
    assert types_by_user == {
        "admin-following": "NEW_CHAPTER",
        "reader": "NEW_CHAPTER",
        "admin-not-following": "CRAWLER_NEW_CHAPTER",
    }


def test_translation_completion_refreshes_existing_notification_message() -> None:
    remote = FakeSupabase()
    discovery = {
        "id": "discovery-1", "novel_id": 15, "chapter_number": 2315,
        "chapter_title": "Source title", "source_url": "https://source.test/chapter/2315",
    }
    novel = {"slug": "novel-15", "title": "Novel 15"}

    assert notify_chapter_discovery(remote, discovery, novel) == 3
    translated = {**discovery, "chapter_title": "Translated chapter title"}
    assert notify_chapter_discovery(remote, translated, novel, update_existing=True) == 0
    assert {event["message"] for event in remote.events.values()} == {"Translated chapter title"}


def test_crawled_chapter_records_discovery_but_waits_for_translation() -> None:
    discovery = {"id": "discovery-2", "novel_id": 15}
    recorded: list[dict] = []

    class CrawlRemote:
        def request(self, method: str, path: str, *, json: dict, headers: dict) -> SimpleNamespace:
            assert method == "POST"
            assert path.endswith("on_conflict=novel_id,source_identity")
            assert headers["Prefer"] == "resolution=ignore-duplicates,return=representation"
            assert json["source_identity"] == "url:https://source.test/chapter/2316"
            assert json["notification_after_translation"] is True
            recorded.append(json)
            return SimpleNamespace(json=lambda: [discovery])

    novel = {"id": 15, "slug": "novel-15", "title": "Novel 15"}
    chapter = {
        "chapter_number": 2316,
        "title": "Chapter 2316",
        "source_url": "https://source.test/chapter/2316/",
    }

    assert record_crawled_chapter_discovery(CrawlRemote(), chapter, novel) == discovery
    assert len(recorded) == 1


def test_translation_completion_updates_existing_source_notification() -> None:
    remote = FakeSupabase()
    discovery = {
        "id": "discovery-3", "novel_id": 15, "chapter_number": 2317,
        "chapter_title": "Chapter 2317", "source_url": "https://source.test/chapter/2317",
    }
    novel = {"slug": "novel-15", "title": "Novel 15"}

    assert notify_chapter_discovery(remote, discovery, novel) == 3
    translated = {**discovery, "chapter_title": "Trận chiến cuối cùng"}
    assert notify_chapter_discovery(remote, translated, novel, update_existing=True) == 0
    assert {event["message"] for event in remote.events.values()} == {"Trận chiến cuối cùng"}
