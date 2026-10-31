"""
Модель DownloadFile — файл загрузки, привязанный к раздаче (Release).

Статус файла загрузки каскадно определяет статус связанных объектов:
  - главы (Chapter) → статус книги (Book)
  - обложки книги (Book.cover_download_file) → статус книги

Если обложка получена без обращения к релизу (онлайн), то для неё объект
DownloadFile НЕ создаётся.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import DownloadFileStatus

if TYPE_CHECKING:
    from app.db.models.release import Release

log = logging.getLogger(__name__)


class DownloadFile(Base):
    """
    Файл загрузки — отслеживает статус одного файла внутри торрента.

    Привязан к раздаче (release_id). Идентифицируется в рамках раздачи
    по file_name (rel_path из торрента).
    """
    __tablename__ = "download_files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('absent','downloading','partial','ready','error')",
            name="ck_download_file_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=False, index=True)

    file_name:    Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes:   Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    downloaded_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    progress:     Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pieces_total: Mapped[int | None] = mapped_column(Integer)
    pieces_have:  Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(
        String(20), default=DownloadFileStatus.ABSENT.value,
        nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    last_modified_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False)

    # ---------- родитель ----------

    release: Mapped["Release"] = relationship(back_populates="download_files")

    # ---------- поведение ----------

    def sync_from_torrent(self,
                          progress: float,
                          is_complete: bool,
                          downloaded_bytes: int | None = None,
                          pieces_total: int | None = None,
                          pieces_have: int | None = None,
                          ) -> bool:
        """Обновляет статус по данным торрент-клиента.

        Returns:
            True, если статус изменился.
        """
        from app.db.models.enums import DownloadFileStatus as S

        old_status = self.status

        self.progress = progress
        if downloaded_bytes is not None:
            self.downloaded_bytes = downloaded_bytes
        if pieces_total is not None:
            self.pieces_total = pieces_total
        if pieces_have is not None:
            self.pieces_have = pieces_have

        if is_complete or progress >= 1.0:
            self.status = S.READY.value
        elif progress > 0:
            self.status = S.DOWNLOADING.value if progress < 1.0 else S.PARTIAL.value
        else:
            self.status = S.ABSENT.value

        self.last_modified_at = datetime.utcnow()

        return self.status != old_status

    def set_status(self, status: str, *,
                   error_message: str | None = None) -> None:
        """Принудительно выставляет статус (например, при ошибке)."""
        self.status = status
        if error_message is not None:
            self.error_message = error_message
        self.last_modified_at = datetime.utcnow()