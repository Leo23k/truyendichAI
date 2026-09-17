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
