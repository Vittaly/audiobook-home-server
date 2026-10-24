from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import BookStatus, ContentType

if TYPE_CHECKING:
    from app.db.models.book import Book
    from app.db.models.chapter import Chapter
    from app.db.models.person import ReleasePerson
    from app.db.models.rubric import ReleaseRubric


class Release(Base):
    """
    Карточка раздачи. Содержит одну или несколько книг (Book, 1:N).
    Поля карточки (title, description, year, language, persons, rubrics,
    cover, status) живут здесь; статус — агрегат над статусами книг,
    пересчитывается методом recompute_status.
    """
    __tablename__ = "release"
    __table_args__ = (
        UniqueConstraint("title_norm", "year", name="ux_release_title_year"),
        CheckConstraint(
            "content_type IN ('single','series','collection')",
            name="ck_release_content_type",
        ),
        CheckConstraint(
            "status IN ('announced','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_release_status",
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

    title:        Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm:   Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    title_raw:    Mapped[str | None] = mapped_column(String(500))
    description:  Mapped[str | None] = mapped_column(Text)
    year:         Mapped[int | None] = mapped_column(Integer, index=True)
    language:     Mapped[str | None] = mapped_column(String(8))

    size_bytes:   Mapped[int | None] = mapped_column(BigInteger)

    content_type: Mapped[str] = mapped_column(
        String(20), default=ContentType.SINGLE.value, nullable=False)
    books_hint:   Mapped[int | None] = mapped_column(Integer)
    folder_map:   Mapped[str | None] = mapped_column(Text)

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

    # Время индексации карточки на сервере (момент записи в БД).
    added_at:      Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)
    # Дата создания раздачи на трекере (парсится со страницы поиска).
    # Отдельно от added_at: added_at — когда мы проиндексировали, released_at —
    # когда раздача появилась на трекере. Легаси-записи (без даты трекера)
    # заполняются значением added_at при миграции схемы в init_db.
    released_at:   Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_seen_at:  Mapped[datetime | None] = mapped_column(DateTime)

    books:    Mapped[list["Book"]] = relationship(
        back_populates="release", cascade="all, delete-orphan",
        order_by="Book.ordinal")
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="release")
    persons:  Mapped[list["ReleasePerson"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")
    rubrics:  Mapped[list["ReleaseRubric"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")

    # ---------- поведение ----------

    def recompute_status(self) -> None:
        """Пересчитывает статус карточки по статусам книг."""
        statuses = [b.status for b in self.books]
        new = self._aggregate(statuses)

        if self.status == new:
            return

        self.status = new
        if new == BookStatus.READY.value:
            self.downloaded_at = datetime.utcnow()

    @staticmethod
    def _aggregate(statuses: list[str]) -> str:
        if not statuses:
            return BookStatus.ANNOUNCED.value

        if all(s == BookStatus.READY.value for s in statuses):
            return BookStatus.READY.value
        if all(s == BookStatus.DELETED.value for s in statuses):
            return BookStatus.DELETED.value
        if any(s == BookStatus.ERROR.value for s in statuses):
            return BookStatus.ERROR.value

        has_ready = any(s == BookStatus.READY.value for s in statuses)
        has_in_progress = any(
            s in (BookStatus.DOWNLOADING.value, BookStatus.PARTIAL.value)
            for s in statuses
        )

        if has_in_progress:
            return (BookStatus.PARTIAL.value if has_ready
                    else BookStatus.DOWNLOADING.value)
        if has_ready:
            return BookStatus.PARTIAL.value
        return BookStatus.ANNOUNCED.value
