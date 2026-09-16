from dataclasses import dataclass
import json
import re
from urllib.parse import urljoin, urlparse


CHAPTER_FOOTER_LINES = {
    ">>章節報錯<<",
    ">>章节报错<<",
    "上一章",
    "目錄",
    "目录",
    "下一章",
    "首頁",
    "首页",
    "電腦版",
    "电脑版",
    "書架",
    "书架",
    "小說大全",
    "小说大全",
    "熱門小說",
    "热门小说",
}

CHAPTER_NOISE_PATTERNS = (
    re.compile(r"^[（(]?\s*本章完\s*[)）]?$"),
    re.compile(r"^請收藏本站[:：]?"),
    re.compile(r"^请收藏本站[:：]?"),
    re.compile(r"^手機版閱讀[:：]?"),
    re.compile(r"^手机版阅读[:：]?"),
)

LOAD_MORE_TEXT_RE = re.compile(r"(下一|下页|下一頁|更多|load\s*more|more)", re.I)
IMAGE_URL_RE = re.compile(r"""(?P<url>(?:https?:)?//[^"'\\\s<>]+?\.(?:jpg|jpeg|png|webp|gif)|/[^"'\\\s<>]+?\.(?:jpg|jpeg|png|webp|gif))""", re.I)


def clean_chapter_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", raw_line).strip()
        if not line:
            continue
        if line in CHAPTER_FOOTER_LINES:
            break
        if any(pattern.search(line) for pattern in CHAPTER_NOISE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def absolute_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value or value.startswith("data:"):
        return None
    return urljoin(base_url, value)


def extract_cover_url(soup, base_url: str) -> str | None:
    for selector, attr in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
    ):
        node = soup.select_one(selector)
        cover_url = absolute_url(base_url, node.get(attr) if node else None)
        if cover_url:
            return cover_url

    cover_url = _json_ld_image_url(soup, base_url)
    if cover_url:
        return cover_url

    for node in soup.select("img[src], img[data-src], img[data-original], img[data-lazy-src]"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            cover_url = absolute_url(base_url, node.get(attr))
            if cover_url:
                return cover_url

    return _embedded_image_url(soup, base_url)


def _json_ld_image_url(soup, base_url: str) -> str | None:
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cover_url = _image_from_json_ld(data, base_url)
        if cover_url:
            return cover_url
    return None


def _image_from_json_ld(data, base_url: str) -> str | None:
    if isinstance(data, list):
        for item in data:
            cover_url = _image_from_json_ld(item, base_url)
            if cover_url:
                return cover_url
        return None
    if not isinstance(data, dict):
        return None
    image = data.get("image") or data.get("thumbnailUrl") or data.get("thumbnail")
    if isinstance(image, str):
        return absolute_url(base_url, image)
    if isinstance(image, list):
        for item in image:
            cover_url = _image_from_json_ld({"image": item}, base_url)
            if cover_url:
                return cover_url
    if isinstance(image, dict):
        return absolute_url(base_url, image.get("url") or image.get("@id"))
    graph = data.get("@graph")
    if graph:
        return _image_from_json_ld(graph, base_url)
    return None


def _embedded_image_url(soup, base_url: str) -> str | None:
    html = str(soup)
    for match in IMAGE_URL_RE.finditer(html):
        cover_url = absolute_url(base_url, match.group("url"))
        if cover_url:
            return cover_url
    return None


@dataclass
class BookInfo:
    source_url: str
    source_name: str
    source_book_id: str | None
    title: str
    author: str | None
    description: str | None
    cover_url: str | None


@dataclass
class ChapterInfo:
    chapter_number: int
    source_chapter_id: str | None
    title: str
    url: str


class BaseCrawler:
    source_name: str
    domains: tuple[str, ...]

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    async def get_book_info(self, url: str) -> BookInfo:
        raise NotImplementedError

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        raise NotImplementedError

    async def get_chapter_content(self, url: str) -> str:
        raise NotImplementedError

    async def search(self, query: str) -> list[BookInfo]:
        raise NotImplementedError


def dedupe_chapters(chapters: list[ChapterInfo]) -> list[ChapterInfo]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[ChapterInfo] = []
    for chapter in chapters:
        chapter_id = chapter.source_chapter_id or ""
        normalized_url = chapter.url.rstrip("/")
        if chapter_id and chapter_id in seen_ids:
            continue
        if normalized_url in seen_urls:
            continue
        if chapter_id:
            seen_ids.add(chapter_id)
        seen_urls.add(normalized_url)
        unique.append(chapter)
    return sorted(unique, key=lambda chapter: chapter.chapter_number)


def pagination_request_urls(soup, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for node in soup.select("a[href], button, [data-url], [data-href], [data-ajax-url], [data-next-url]"):
        text = node.get_text(" ", strip=True)
        marker = " ".join(
            value
            for value in (
                text,
                node.get("class") and " ".join(node.get("class")),
                node.get("id"),
                node.get("rel") and " ".join(node.get("rel")),
            )
            if value
        )
        if not LOAD_MORE_TEXT_RE.search(marker):
            continue
        for attr in ("href", "data-url", "data-href", "data-ajax-url", "data-next-url"):
            value = node.get(attr)
            if not value or value.startswith("#") or value.lower().startswith("javascript:"):
                continue
            href = value if value.startswith(("http://", "https://")) else urljoin(base_url, value)
            normalized = href.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(href)
    return urls
