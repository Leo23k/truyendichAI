class CrawlerError(Exception):
    """An expected crawler failure safe to show as a Vietnamese UI message."""


class UnsupportedSourceError(CrawlerError):
    pass


class InvalidSourceUrlError(CrawlerError):
    pass


class SourceRequestError(CrawlerError):
    pass


class SourceParseError(CrawlerError):
    pass
