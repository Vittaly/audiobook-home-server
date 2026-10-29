from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.book import Book
    from app.catalog.covers import CoverService

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    """Нижний регистр + схлопывание пробелов."""
    return " ".join(s.lower().split())


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
    # CRUD methods
    # ============================================================

    @classmethod
    def find_or_create(cls, db: Session, name: str) -> "Series":
        """Находит серию по нормализованному имени или создаёт новую.

        Args:
            db:   открытая сессия.
            name: название серии (будет нормализовано для поиска).

        Returns:
            Существующий или новый экземпляр Series (уже добавлен в сессию).
        """
        name_norm = _norm(name)
        series = db.execute(
            select(cls).where(cls.title_norm == name_norm)
        ).scalar_one_or_none()
        if series is None:
            series = cls(title=name, title_norm=name_norm)
            db.add(series)
            db.flush()
            log.info("series: создана новая %r", name)
        return series

    # ============================================================
    # Загрузка всех книг серии
    # ============================================================

    async def start_download(
        self,
        db: Session,
        covers: "CoverService | None" = None,
    ) -> int:
        """Запускает загрузку всех книг серии.

        Проходит по self.books (ORDER BY series_order) и вызывает
        book.start_download() для каждой книги у которой статус
        не READY.

        Args:
            db:     открытая сессия (caller отвечает за commit).
            covers: CoverService (опционально, для обложек).

        Returns:
            Количество книг, для которых запущена загрузка.
        """
        from app.catalog.types import DownloadResult

        started = 0
        sorted_books = sorted(
            [b for b in self.books if b.status != "ready"],
            key=lambda b: b.series_order or 0,
        )

        for book in sorted_books:
            result: DownloadResult = await book.start_download(
                db=db, covers=covers)
            if result.success:
                started += 1

        log.info("series=%s: запущено %d книг из %d",
                 self.title, started, len(sorted_books))
        return started