from __future__ import annotations

from types import SimpleNamespace

from app.notification_service import notify_chapter_discovery


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
