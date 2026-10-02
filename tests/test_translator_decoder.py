from infra.translator.service import clean_decoded_text, is_punctuation_only


def test_clean_decoded_text_removes_generic_decoder_artifacts() -> None:
    output = '""\nPKim Túc8> Ôn\nQuốc sư> Toán không bỏ sót kế sách!\nPĐều nghe.4>'

    assert clean_decoded_text(output) == "Kim Túc Ôn\nQuốc sư Toán không bỏ sót kế sách!\nĐều nghe."


def test_punctuation_only_source_lines_are_not_sent_to_the_model() -> None:
    assert is_punctuation_only("。")
    assert is_punctuation_only("…")
    assert is_punctuation_only('  \"\"  ')
    assert not is_punctuation_only("Hắn xoay người.")
