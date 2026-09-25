from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo
from app.models import Book, Chapter
from app.translator.service import TranslationUnavailableError, translator


class BookService:
    def __init__(self, db: Session, crawler: BaseCrawler) -> None:
        self.db = db
        self.crawler = crawler

    async def import_book(self, url: str) -> tuple[Book, int]:
        info = await self.crawler.get_book_info(url)
        book = self.db.scalar(select(Book).where(Book.source_url == info.source_url))
        if book is None:
            book = Book(
                source_url=info.source_url,
                source_name=info.source_name,
                source_book_id=info.source_book_id,
                title=info.title,
                author=info.author,
                description=info.description,
                cover_url=info.cover_url,
            )
            self.db.add(book)
            self.db.flush()
        chapters_added = await self._sync_chapters(book, await self.crawler.get_chapters(url))
        self._translate_metadata(book)
        self.db.commit()
        self.db.refresh(book)
        return book, chapters_added

    async def update_book(self, book: Book) -> int:
        chapters_added = await self._sync_chapters(book, await self.crawler.get_chapters(book.source_url))
        self._translate_metadata(book)
        book.last_checked_at = datetime.utcnow()
        self.db.commit()
        return chapters_added

    async def _sync_chapters(self, book: Book, incoming: list[ChapterInfo]) -> int:
        existing = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id)).all()
        by_source_id = {chapter.source_chapter_id: chapter for chapter in existing if chapter.source_chapter_id}
        by_url = {chapter.url: chapter for chapter in existing}
        added = 0
        new_chapters: list[Chapter] = []
        for info in incoming:
            if info.source_chapter_id in by_source_id or info.url in by_url:
                continue
            chapter = Chapter(
                    book_id=book.id,
                    chapter_number=info.chapter_number,
                    source_chapter_id=info.source_chapter_id,
                    title=info.title,
                    url=info.url,
                )
            self.db.add(chapter)
            new_chapters.append(chapter)
            added += 1
        self._translate_new_titles(new_chapters)
        self._translate_missing_titles(existing)
        return added

    def _translate_metadata(self, book: Book) -> None:
        try:
            if not book.translated_title:
                book.translated_title = translator.translate_metadata(book.title, self.db)
            if book.author and not book.translated_author:
                book.translated_author = translator.translate_metadata(book.author, self.db)
            if book.description and not book.translated_description:
                book.translated_description = translator.translate(book.description, self.db)
        except TranslationUnavailableError:
            return

    def _translate_new_titles(self, chapters: list[Chapter]) -> None:
        for chapter in chapters:
            try:
                chapter.translated_title = translator.translate(chapter.title, self.db)
            except TranslationUnavailableError:
                return

    def _translate_missing_titles(self, chapters: list[Chapter]) -> None:
        for chapter in chapters:
            if chapter.translated_title:
                continue
            try:
                chapter.translated_title = translator.translate(chapter.title, self.db)
            except TranslationUnavailableError:
                return
            except Exception:
                return
