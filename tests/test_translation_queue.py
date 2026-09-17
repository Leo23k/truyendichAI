from sync import service
from sync.service import enqueue_untranslated_chapters


class FakeRemote:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict], str]] = []

    def rows(self, _table: str, _params: dict[str, str]) -> list[dict]:
        return [{"chapter_id": 2}]

    def insert_many_ignore(self, table: str, payloads: list[dict], conflict: str) -> None:
        self.inserted.append((table, payloads, conflict))


def test_enqueue_creates_chunks_only_for_new_untranslated_chapters() -> None:
    remote = FakeRemote()
    count = enqueue_untranslated_chapters(remote, [
        {"id": 1, "original_content": "Nội dung." * 250, "translated_content": None},
        {"id": 2, "original_content": "Đã có queue.", "translated_content": None},
        {"id": 3, "original_content": "Đã dịch.", "translated_content": "Done"},
    ])

    assert count == 1
    chunks, queue = remote.inserted
    assert chunks[0] == "chapter_chunks"
    assert {row["chapter_id"] for row in chunks[1]} == {1}
    assert queue[0] == "translation_queue"
    assert queue[1][0]["status"] == "queued"
    assert queue[1][0]["total_chunks"] == len(chunks[1])


def test_worker_translates_chunks_one_at_a_time(monkeypatch) -> None:
    class WorkerRemote:
        def __init__(self) -> None:
            self.job = {"id": "job", "chapter_id": 7, "current_chunk": 0, "total_chunks": 2}
            self.chunks = [{"id": "a", "original_text": "Một."}, {"id": "b", "original_text": "Hai."}]
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "translation_queue":
                return [self.job]
            return [self.chunks[self.job["current_chunk"]]]

        def patch_rows(self, table: str, _params: dict[str, str], payload: dict) -> list[dict]:
            self.patches.append((table, _params, payload))
            if table == "translation_queue" and payload.get("status") == "translating":
                return [self.job]
            return []

    remote = WorkerRemote()
    monkeypatch.setattr(service.translator, "translate", lambda text: f"Dịch {text}")

    assert service.run_translation_worker_once(remote) is True
    assert [patch[2]["translated_text"] for patch in remote.patches if patch[0] == "chapter_chunks"] == ["Dịch Một.", "Dịch Hai."]
    assert [patch[0] for patch in remote.patches].index("chapter_chunks") < [patch[0] for patch in remote.patches].index("translation_queue", 1)
    assert remote.patches[-1][2]["status"] == "completed"
