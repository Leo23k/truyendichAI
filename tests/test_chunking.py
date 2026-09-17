from app.translator.chunking import chunk_text


def test_chunking_keeps_sentence_boundaries_within_range() -> None:
    text = "\n".join("Đây là câu hoàn chỉnh." * 80 for _ in range(6))
    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert all(len(chunk) <= 1800 for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)


def test_chunking_splits_a_sentence_that_exceeds_maximum() -> None:
    chunks = chunk_text("a" * 2000)

    assert [len(chunk) for chunk in chunks] == [1800, 200]
