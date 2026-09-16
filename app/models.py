from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Book(TimestampMixin, Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_book_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    translated_title: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(255))
    translated_author: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    translated_description: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime)

    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Chapter.chapter_number"
    )


class Chapter(TimestampMixin, Base):
    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint("book_id", "source_chapter_id", name="uq_chapter_source_id"),
        UniqueConstraint("book_id", "url", name="uq_chapter_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_chapter_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    translated_title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    original_content: Mapped[str | None] = mapped_column(Text)
    translated_content: Mapped[str | None] = mapped_column(Text)

    book: Mapped[Book] = relationship(back_populates="chapters")


class TranslationMemory(TimestampMixin, Base):
    __tablename__ = "translation_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_text: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    replacement_text: Mapped[str] = mapped_column(String(500), nullable=False)


class AudioCache(Base):
    __tablename__ = "audio_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    voice: Mapped[str] = mapped_column(String(120), nullable=False)
    audio_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("chapter_id", "content_hash", "voice", name="uq_audio_cache_key"),)
