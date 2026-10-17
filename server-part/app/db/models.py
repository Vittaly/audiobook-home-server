from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ============================================================
# ENUM-Ы
# ============================================================

class BookStatus(str, enum.Enum):
    ANNOUNCED   = "announced"
    DOWNLOADING = "downloading"
    PARTIAL     = "partial"
    READY       = "ready"
    DELETING    = "deleting"
    DELETED     = "deleted"
    ERROR       = "error"


class ChapterStatus(str, enum.Enum):
    ABSENT      = "absent"
    DOWNLOADING = "downloading"
    PARTIAL     = "partial"
    READY       = "ready"
    DELETING    = "deleting"
    DELETED     = "deleted"
    ERROR       = "error"


class ContentType(str, enum.Enum):
    SINGLE     = "single"
    SERIES     = "series"
    COLLECTION = "collection"


class CoverOrigin(str, enum.Enum):
    TRACKER = "tracker"
    TORRENT = "torrent"
    SERIES  = "series"
    UPLOAD  = "upload"


class PersonRole(str, enum.Enum):
    AUTHOR      = "author"
    NARRATOR    = "narrator"
    TRANSLATOR  = "translator"
    EDITOR      = "editor"
    ILLUSTRATOR = "illustrator"


class RubricKind(str, enum.Enum):
    GENRE  = "genre"
    TAG    = "tag"
    RUBRIC = "rubric"


# ============================================================
# СПРАВОЧНИКИ
# ============================================================

class Source(Base):
    """Трекер-источник."""
    __tablename__ = "source"

    id:       Mapped[int] = mapped_column(Integer, primary_key=True)
    name:     Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)


class Person(Base):
    """Автор, чтец, переводчик и т.п."""
    __tablename__ = "person"

    id:        Mapped[int] = mapped_column(Integer, primary_key=True)
    name:      Mapped[str] = mapped_column(String(255), nullable=False)
    name_norm: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True)

    books: Mapped[list["BookPerson"]] = relationship(
        back_populates="person", cascade="all, delete-orphan")


class Rubric(Base):
    """Жанр / тег / рубрика."""
    __tablename__ = "rubric"
    __table_args__ = (
        UniqueConstraint("kind", "title_norm", name="ux_rubric_kind_title"),
        CheckConstraint("kind IN ('genre','tag','rubric')", name="ck_rubric_kind"),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    kind:       Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title:      Mapped[str] = mapped_column(String(255), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(255), nullable=False)

    books: Mapped[list["BookRubric"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan")


class Series(Base):
    """Цикл книг."""
    __tablename__ = "series"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    title:      Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm: Mapped[str] = mapped_column(
        String(500), nullable=False, unique=True, index=True)

    cover_source_url: Mapped[str | None] = mapped_column(Text)
    cover_cache_path: Mapped[str | None] = mapped_column(Text)
    cover_mime:       Mapped[str | None] = mapped_column(String(50))
    cover_sha256:     Mapped[str | None] = mapped_column(String(64))

    books: Mapped[list["Book"]] = relationship(back_populates="series")


# ============================================================
# КНИГА
# ============================================================

class Book(Base):
    """
    Книга в каталоге. Атрибут — каталог, не источник.
    Статус — агрегат над статусами глав.
    """
    __tablename__ = "book"
    __table_args__ = (
        UniqueConstraint("title_norm", "year", name="ux_book_title_year"),
        CheckConstraint(
            "status IN ('announced','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_book_status",
        ),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    title:      Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    title_raw:  Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    year:       Mapped[int | None] = mapped_column(Integer, index=True)
    language:   Mapped[str | None] = mapped_column(String(8))

    series_id:    Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"))
    series_order: Mapped[int | None] = mapped_column(Integer)

    status:        Mapped[str] = mapped_column(
        String(20), default=BookStatus.ANNOUNCED.value,
        nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    cover_origin:      Mapped[str | None] = mapped_column(String(20))
    cover_source_url:  Mapped[str | None] = mapped_column(Text)
    cover_cache_path:  Mapped[str | None] = mapped_column(Text)
    cover_mime:        Mapped[str | None] = mapped_column(String(50))
    cover_sha256:      Mapped[str | None] = mapped_column(String(64), index=True)
    cover_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    added_at:      Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    series:   Mapped["Series | None"] = relationship(back_populates="books")
    releases: Mapped[list["BookRelease"]] = relationship(
        back_populates="book", cascade="all, delete-orphan")
    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan",
        order_by="Chapter.ordinal")
    persons:  Mapped[list["BookPerson"]] = relationship(
        back_populates="book", cascade="all, delete-orphan")
    rubrics:  Mapped[list["BookRubric"]] = relationship(
        back_populates="book", cascade="all, delete-orphan")


class BookPerson(Base):
    __tablename__ = "book_person"
    __table_args__ = (
        CheckConstraint(
            "role IN ('author','narrator','translator','editor','illustrator')",
            name="ck_book_person_role",
        ),
    )

    book_id:   Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), primary_key=True)
    role:      Mapped[str] = mapped_column(String(20), primary_key=True)
    ordinal:   Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    book:   Mapped[Book]   = relationship(back_populates="persons")
    person: Mapped[Person] = relationship(back_populates="books")


class BookRubric(Base):
    __tablename__ = "book_rubric"

    book_id:   Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), primary_key=True)
    rubric_id: Mapped[int] = mapped_column(
        ForeignKey("rubric.id", ondelete="CASCADE"), primary_key=True)

    book:   Mapped[Book]   = relationship(back_populates="rubrics")
    rubric: Mapped[Rubric] = relationship(back_populates="books")


# ============================================================
# РАЗДАЧА (ИСТОЧНИК)
# ============================================================

class Release(Base):
    """
    Раздача-источник. Не привязана к одной книге.
    Одна раздача может содержать много книг (series / collection).
    Связь с книгами — через BookRelease.
    """
    __tablename__ = "release"
    __table_args__ = (
        CheckConstraint(
            "content_type IN ('single','series','collection')",
            name="ck_release_content_type",
        ),
    )

    id:        Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("source.id", ondelete="SET NULL"))

    torrent_hash: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True)
    magnet:       Mapped[str] = mapped_column(Text, nullable=False)
    source_url:   Mapped[str | None] = mapped_column(Text)
    save_path:    Mapped[str] = mapped_column(Text, nullable=False)

    title_raw:  Mapped[str | None] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    content_type: Mapped[str] = mapped_column(
        String(20), default=ContentType.SINGLE.value, nullable=False)
    books_hint:   Mapped[int | None] = mapped_column(Integer)
    folder_map:   Mapped[str | None] = mapped_column(Text)
    # ↑ JSON-план разбора: [{"folder": "01. ...", "title": "...", "order": 1}, ...]

    added_at:     Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    books:    Mapped[list["BookRelease"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="release")


class BookRelease(Base):
    """
    M:N между книгой и раздачей.
    MVP: одна запись на пару (book, release), is_primary=1.
    Полный разбор мультикнижных: N записей на одну раздачу.
    """
    __tablename__ = "book_release"

    book_id:    Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), primary_key=True)

    is_primary: Mapped[bool] = mapped_column(
        Integer, default=1, nullable=False)
    # ↑ 1 — главы этой книги берутся из этой раздачи

    local_title: Mapped[str | None] = mapped_column(String(500))
    # ↑ как книга названа внутри раздачи («01. Девятиэтажка»)

    local_order: Mapped[int | None] = mapped_column(Integer)
    # ↑ порядковый номер книги в раздаче

    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False)

    book:    Mapped[Book]    = relationship(back_populates="releases")
    release: Mapped[Release] = relationship(back_populates="books")


# ============================================================
# ГЛАВА
# ============================================================

class Chapter(Base):
    """
    Глава = один аудиофайл внутри раздачи.
    Связана и с книгой (какой книге принадлежит),
    и с раздачей (откуда пришла).
    """
    __tablename__ = "chapter"
    __table_args__ = (
        UniqueConstraint("book_id", "ordinal", name="ux_chapter_ordinal"),
        CheckConstraint(
            "status IN ('absent','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_chapter_status",
        ),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id:    Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False, index=True)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    # расположение внутри торрента
    file_index:  Mapped[int] = mapped_column(Integer, nullable=False)
    rel_path:    Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes:  Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_offset: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # статус
    status:           Mapped[str] = mapped_column(
        String(20), default=ChapterStatus.ABSENT.value,
        nullable=False, index=True)
    progress:         Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pieces_total:     Mapped[int | None] = mapped_column(Integer)
    pieces_have:      Mapped[int | None] = mapped_column(Integer)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_message:    Mapped[str | None] = mapped_column(Text)

    # метаданные
    title:        Mapped[str | None] = mapped_column(String(500))
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    # для TTL и стриминга
    last_streamed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_modified_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False)

    book:    Mapped[Book]    = relationship(back_populates="chapters")
    release: Mapped[Release] = relationship(back_populates="chapters")


# ============================================================
# ЖУРНАЛ ОБРАЩЕНИЙ
# ============================================================

class AccessLog(Base):
    __tablename__ = "access_log"
    __table_args__ = (
        CheckConstraint(
            "action IN ('list','detail','stream','download','delete')",
            name="ck_access_action",
        ),
    )

    id:          Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id:     Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_id:  Mapped[int | None] = mapped_column(
        ForeignKey("chapter.id", ondelete="SET NULL"))
    action:      Mapped[str] = mapped_column(String(20), nullable=False)
    client_ip:   Mapped[str | None] = mapped_column(String(45))
    user_agent:  Mapped[str | None] = mapped_column(String(255))
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)