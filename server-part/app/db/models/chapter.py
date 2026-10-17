from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import ChapterStatus

if TYPE_CHECKING:
    from app.db.models.book import Book
    from app.db.models.release import Release


class Chapter(Base):
    """
    Глава = один аудиофайл внутри раздачи.
    При изменении статуса каскадно пересчитывает статус книги.
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

    ordinal:     Mapped[int] = mapped_column(Integer, nullable=False)
    file_index:  Mapped[int] = mapped_column(Integer, nullable=False)
    rel_path:    Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes:  Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_offset: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    status:           Mapped[str] = mapped_column(
        String(20), default=ChapterStatus.ABSENT.value,
        nullable=False, index=True)
    progress:         Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pieces_total:     Mapped[int | None] = mapped_column(Integer)
    pieces_have:      Mapped[int | None] = mapped_column(Integer)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_message:    Mapped[str | None] = mapped_column(Text)

    title:        Mapped[str | None] = mapped_column(String(500))
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    last_streamed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_modified_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False)

    book:    Mapped["Book"]    = relationship(back_populates="chapters")
    release: Mapped["Release"] = relationship(back_populates="chapters")

    # ---------- поведение ----------

    def set_status(self, status: str, *,
                   progress: float | None = None,
                   pieces_have: int | None = None,
                   downloaded_bytes: int | None = None,
                   error_message: str | None = None) -> None:
        """Меняет статус и каскадно пересчитывает статус книги."""
        self.status = status
        if progress is not None:
            self.progress = progress
        if pieces_have is not None:
            self.pieces_have = pieces_have
        if downloaded_bytes is not None:
            self.downloaded_bytes = downloaded_bytes
        if error_message is not None:
            self.error_message = error_message
        self.last_modified_at = datetime.utcnow()

        if self.book is not None:
            self.book.recompute_status()

    def mark_ready(self) -> None:
        self.set_status(ChapterStatus.READY.value, progress=1.0)

    def mark_progress(self, progress: float,
                      pieces_have: int | None = None,
                      downloaded_bytes: int | None = None) -> None:
        status = (ChapterStatus.PARTIAL.value if 0 < progress < 1
                  else ChapterStatus.DOWNLOADING.value)
        self.set_status(status, progress=progress,
                        pieces_have=pieces_have,
                        downloaded_bytes=downloaded_bytes)

    def mark_error(self, message: str) -> None:
        self.set_status(ChapterStatus.ERROR.value, error_message=message)

    def mark_deleted(self) -> None:
        self.set_status(ChapterStatus.DELETED.value,
                        progress=0.0, downloaded_bytes=0)

    def mark_streamed(self) -> None:
        self.last_streamed_at = datetime.utcnow()