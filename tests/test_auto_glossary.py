from sync.service import _auto_name_candidates, is_allowed_auto_glossary_entry


def test_auto_glossary_whitelists_only_named_terms() -> None:
    source = "张三先生说，人称『青衣客』，出身青云宗，修炼太上剑诀。那两位、李家二、司的郎中、麻烦、那位宋先生都不能收录。"

    candidates = _auto_name_candidates(source)

    assert {"张三", "青衣客", "青云宗", "太上剑诀"} <= candidates
    assert not {"那两位", "李家二", "司的郎中", "麻烦", "那位宋"} & candidates


def test_existing_auto_glossary_rejects_non_entities() -> None:
    assert is_allowed_auto_glossary_entry("张三")
    assert is_allowed_auto_glossary_entry("青衣客")
    assert is_allowed_auto_glossary_entry("青云宗")
    assert is_allowed_auto_glossary_entry("太上剑诀")
    assert not is_allowed_auto_glossary_entry("那两位")
    assert not is_allowed_auto_glossary_entry("李家二")
    assert not is_allowed_auto_glossary_entry("司的郎中")
    assert not is_allowed_auto_glossary_entry("麻烦")
