from __future__ import annotations

import app.cdn as cdn
import app.chapter_monitor as chapter_monitor


def test_admin_novel_rows_include_untranslated_chapter_count(monkeypatch) -> None:
    def fake_service_rows(table: str, params: dict[str, str]) -> list[dict]:
        assert table == "novels"
        assert params["untranslated_chapter_rows.translated"] == "eq.false"
        return [{
            "id": 1,
            "title": "Novel",
            "untranslated_chapter_rows": [{"count": 7}],
        }]

    monkeypatch.setattr(cdn, "_service_rows", fake_service_rows)

    assert cdn.load_admin_novels()[0]["untranslated_chapters"] == 7


def test_chapter_monitor_checks_only_ongoing_novels(monkeypatch) -> None:
    class FakeCrawler:
        async def get_latest_chapters(self, url: str, limit: int) -> list:
            return []

    class FakeRemote:
        def all_rows(self, table: str, params: dict[str, str]) -> list[dict]:
            if table == "novels":
                assert params["reading_status"] == "eq.ongoing"
                return [{"id": 1, "source_url": "https://source.test/book", "source_type": "zh"}]
            if table == "chapter_monitor_state":
                return [{"novel_id": 1, "last_checked_at": "2026-10-14T00:00:00+00:00"}]
            if table == "chapter_discoveries":
                return []
            raise AssertionError(table)

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            assert table == "chapter_monitor_state"
            return []

    monkeypatch.setattr(chapter_monitor, "SupabaseSync", FakeRemote)
    monkeypatch.setattr(chapter_monitor.registry, "get_for_url", lambda url: FakeCrawler())
    monkeypatch.setattr(chapter_monitor, "deliver_pending_push", lambda limit: 0)

    assert chapter_monitor.run_chapter_monitor() == {
        "checked": 1,
        "discovered": 0,
        "errors": 0,
        "push_sent": 0,
    }
