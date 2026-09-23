from __future__ import annotations

import logging
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crawlers.base import BookInfo
from app.crawlers.bootstrap import registry
from app.models import Book
from app.translator.preprocess import traditional_to_simplified

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None


logger = logging.getLogger(__name__)

HAN_VIET_SEARCH = {
    "陣": "tran",
    "阵": "tran",
    "問": "van",
    "问": "van",
    "長": "truong",
    "长": "truong",
    "生": "sinh",
    "玄": "huyen",
    "鑒": "giam",
    "鉴": "giam",
    "仙": "tien",
    "族": "toc",

}


def normalize_search_key(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


def query_variants(query: str) -> list[str]:
    variants: list[str] = []
    for value in (query, traditional_to_simplified(query), simplified_to_traditional(query)):
        value = value.strip()
        if value and value not in variants:
            variants.append(value)
    return variants


def simplified_to_traditional(text: str) -> str:
    if OpenCC is None:
        return text
    return OpenCC("s2t").convert(text)


async def search_books(query: str, db: Session) -> list[BookInfo]:
    logger.info("Search request query=%r", query)
    results: list[BookInfo] = []
    seen: set[str] = set()

    for variant in query_variants(query):
        remote_results = await registry.search(variant)
        logger.info("Search crawler variant=%r results=%s", variant, len(remote_results))
        for book in remote_results:
            keys = result_keys(book)
            if seen.intersection(keys):
                continue
            seen.update(keys)
            results.append(book)

    for book in local_book_matches(query, db):
        keys = result_keys(book)
        if seen.intersection(keys):
            continue
        seen.update(keys)
        results.append(book)

    logger.info("Search response query=%r results=%s", query, len(results))
    return results


def result_keys(book: BookInfo) -> set[str]:
    keys = {f"url:{book.source_url.rstrip('/')}"}
    if book.source_book_id:
        keys.add(f"id:{book.source_name}:{book.source_book_id}")
    if book.cover_url and not book.cover_url.endswith("/noimg.jpg"):
        keys.add(f"cover:{book.cover_url}")
    title_key = compact_key(book.title)
    if title_key:
        keys.add(f"title:{book.source_name}:{title_key}")
    return keys


def compact_key(value: str | None) -> str:
    return re.sub(r"\s+", "", normalize_search_key(value))


def han_viet_search_key(value: str | None) -> str:
    if not value:
        return ""
    words = [HAN_VIET_SEARCH.get(char, char) for char in value]
    return normalize_search_key(" ".join(words))


def local_book_matches(query: str, db: Session) -> list[BookInfo]:
    needle = normalize_search_key(query)
    if not needle:
        return []
    matches: list[BookInfo] = []
    for book in db.scalars(select(Book).order_by(Book.updated_at.desc())).all():
        fields = (
            book.title,
            book.translated_title,
            book.author,
            book.translated_author,
            book.source_book_id,
        )
        haystacks = [normalize_search_key(field) for field in fields]
        haystacks.extend(han_viet_search_key(field) for field in fields)
        if not any(needle in haystack for haystack in haystacks):
            continue
        matches.append(
            BookInfo(
                book.source_url,
                book.source_name,
                book.source_book_id,
                book.translated_title or book.title,
                book.translated_author or book.author,
                book.translated_description or book.description,
                book.cover_url,
            )
        )
    logger.info("Search local query=%r results=%s", query, len(matches))
    return matches
