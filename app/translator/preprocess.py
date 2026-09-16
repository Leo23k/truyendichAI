from functools import lru_cache

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None


@lru_cache(maxsize=1)
def _converter():
    if OpenCC is None:
        return None
    return OpenCC("t2s")


def traditional_to_simplified(text: str) -> str:
    converter = _converter()
    return converter.convert(text) if converter else text
