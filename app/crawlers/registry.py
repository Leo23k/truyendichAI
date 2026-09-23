import ipaddress
import logging
from urllib.parse import urlparse

from app.crawlers.base import BaseCrawler, BookInfo
from app.crawlers.errors import CrawlerError, InvalidSourceUrlError, UnsupportedSourceError


logger = logging.getLogger(__name__)


class CrawlerRegistry:
    def __init__(self) -> None:
        self._crawlers: list[BaseCrawler] = []

    def register(self, crawler: BaseCrawler) -> None:
        self._crawlers.append(crawler)

    def get_for_url(self, url: str) -> BaseCrawler:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidSourceUrlError("URL không hợp lệ. Chỉ hỗ trợ http:// và https://.")
        try:
            host_ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            host_ip = None
        if parsed.hostname == "localhost" or host_ip and (host_ip.is_private or host_ip.is_loopback or host_ip.is_link_local):
            raise InvalidSourceUrlError("Không thể truy cập địa chỉ mạng nội bộ.")
        for crawler in self._crawlers:
            if crawler.can_handle(url):
                return crawler
        raise UnsupportedSourceError("Website này hiện chưa được hỗ trợ.")

    async def search(self, query: str) -> list[BookInfo]:
        results: list[BookInfo] = []
        for crawler in self._crawlers:
            try:
                results.extend(await crawler.search(query))
            except (CrawlerError, NotImplementedError) as exc:
                logger.info("Search source unavailable source=%s query=%r error=%s", crawler.source_name, query, exc)
                continue
        return results


registry = CrawlerRegistry()
