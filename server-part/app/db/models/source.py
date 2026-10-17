from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Source(Base):
    """
    Трекер-источник. Чистый справочник.
    Состояние обхода хранится отдельно — в CrawlState.
    """
    __tablename__ = "source"

    id:       Mapped[int] = mapped_column(Integer, primary_key=True)
    name:     Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)