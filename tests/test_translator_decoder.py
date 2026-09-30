from infra.translator.service import clean_decoded_text


def test_clean_decoded_text_removes_generic_decoder_artifacts() -> None:
    output = '""\nPKim Túc8> Ôn\nQuốc sư> Toán không bỏ sót kế sách!\nPĐều nghe.4>'

    assert clean_decoded_text(output) == "Kim Túc Ôn\nQuốc sư Toán không bỏ sót kế sách!\nĐều nghe."
