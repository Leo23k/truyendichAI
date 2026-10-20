from app.translator.validation import translation_issue


def test_translation_validator_flags_empty_short_echo_and_truncated_results() -> None:
    source = "原文" * 400
    assert translation_issue(source, "") == "empty"
    assert translation_issue(source, "很短") == "too_short"
    assert translation_issue(source, source) == "source_echo"
    assert translation_issue(source, "bản dịch [TRUNCATED]") == "truncated"


def test_translation_validator_flags_prompt_echo_and_duplicate_paragraphs() -> None:
    prompt = "Dịch tự nhiên, giữ nguyên tên riêng và chỉ trả bản dịch.\n\n{{text}}"
    assert translation_issue("原文", "Dịch tự nhiên, giữ nguyên tên riêng và chỉ trả bản dịch.\n\n原文", prompt) == "prompt_echo"
    repeated = "Đoạn dịch được lặp lại ở đây." * 4
    assert translation_issue("x" * 500, f"{repeated}\n\n{repeated}\n\n{repeated}\n\n{repeated}") == "duplicate_output"


def test_translation_validator_accepts_normal_output() -> None:
    assert translation_issue("你好，世界。" * 30, "Xin chào thế giới. " * 20) is None
