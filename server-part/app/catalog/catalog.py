"""
Catalog — stateless репозиторий для кросс-сущностных запросов.

Базовые CRUD (find_or_create) живут как classmethod в самих моделях:
  Person.find_or_create(db, name)
  Rubric.find_or_create(db, kind, title)
  Series.find_or_create(db, name)

Catalog содержит только то, что требует JOIN нескольких таблиц или
специфической логики поиска (например, поиск соседей по серии).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models.book import Book

log = logging.getLogger(__name__)


class Catalog:
    """Stateless-репозиторий.

    Usage::

        peers = Catalog.find_series_peers(db, book)
        books = Catalog.find_books_by_series(db, series_id)
    """

    # ---------------------------------------------------------------
    # Book queries
    # ---------------------------------------------------------------

    @staticmethod
    def get_book(db: Session, book_hash: str) -> "Book | None":
        """Находит книгу по первичному ключу."""
        from app.db.models.book import Book
        return db.get(Book, book_hash)

    @staticmethod
    def find_series_peers(db: Session, book: "Book") -> list["Book"]:
        """Возвращает другие книги этой же серии, отсортированные по порядку.

        Если у книги нет серии — возвращает пустой список.
        """
        if not book.series_id:
            return []
        from app.db.models.book import Book

        result = db.execute(
            select(Book).where(
                Book.series_id == book.series_id,
                Book.book_hash != book.book_hash,
            ).order_by(Book.series_order.nullslast())
        ).scalars().all()
        return list(result)

    @staticmethod
    def find_books_by_series(db: Session, series_id: int) -> list["Book"]:
        """Все книги серии, отсортированные по series_order."""
        from app.db.models.book import Book

        result = db.execute(
            select(Book).where(
                Book.series_id == series_id,
            ).order_by(Book.series_order.nullslast())
        ).scalars().all()
        return list(result)