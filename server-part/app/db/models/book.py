from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import BookStatus, ChapterStatus

if TYPE_CHECKING:
    from app.db.models.chapter import Chapter
    from app.db.models.release import Release
    from app.db.models.series import Series


class Book(Base):
    """
    Книга (том) внутри раздачи. Карточка-раздача (Release) содержит одну или
    несколько книг (1:N). Статус — агрегат над статусами глав, пересчитывается
    методом recompute_status. Серия — атрибут любой книги.
    """
    __tablename__ = "book"
    __table_args__ = (
        UniqueConstraint("release_id", "ordinal", name="ux_book_release_ordinal"),
        CheckConstraint(
            "status IN ('announced','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_book_status",
        ),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False, index=True)

    ordinal:  Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    folder:   Mapped[str | None] = mapped_column(Text)

    title:      Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    title_raw:  Mapped[str | None] = mapped_column(String(500))

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
    # признак временной обложки: показываем обложку раздачи, пока не получена своя
    cover_is_temp:     Mapped[bool] = mapped_column(Integer, default=False, nullable=False)
    # признак «книга ещё не полностью определена»: книга-заглушка, созданная
    # при первом появлении раздачи до скана структуры торрента. Снимается
    # после получения глав/описания (ingest_release), когда данные книги
    # финализируются. Клиент пока видит флаг — поллит книгу и не считает
    # её готовой, а при снятии признака перечитывает.
    is_provisional:    Mapped[bool] = mapped_column(
        Integer, default=False, nullable=False, index=True)
    # rel-путь картинки тома внутри торрента (из структуры), если она есть.
    # Позволяет докачать файл после того, как он появился на диске.
    cover_path_hint:   Mapped[str | None] = mapped_column(Text)

    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    release:  Mapped["Release"] = relationship(back_populates="books")
    series:   Mapped["Series | None"] = relationship(back_populates="books")
    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan",
        order_by="Chapter.ordinal")

    # ---------- поведение ----------

    def recompute_status(self) -> None:
        """Пересчитывает статус книги по главам. Вызывается главой или event'ом."""
        statuses = [c.status for c in self.chapters]
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

        if all(s == ChapterStatus.READY.value for s in statuses):
            return BookStatus.READY.value
        if all(s == ChapterStatus.DELETED.value for s in statuses):
            return BookStatus.DELETED.value
        if any(s == ChapterStatus.ERROR.value for s in statuses):
            return BookStatus.ERROR.value

        has_ready = any(s == ChapterStatus.READY.value for s in statuses)
        has_in_progress = any(
            s in (ChapterStatus.DOWNLOADING.value, ChapterStatus.PARTIAL.value)
            for s in statuses
        )

        if has_in_progress:
            return (BookStatus.PARTIAL.value if has_ready
                    else BookStatus.DOWNLOADING.value)
        if has_ready:
            return BookStatus.PARTIAL.value
        return BookStatus.ANNOUNCED.value
