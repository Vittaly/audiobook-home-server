from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.book import Book


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