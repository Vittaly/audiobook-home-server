from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.source import Source


class CrawlState(Base):
    """
    Состояние обхода конкретного трекера в конкретной категории.
    Одна строка на (source, category). Хранит границу по дате
    и позицию незавершённого первого обхода.
    """
    __tablename__ = "crawl_state"
    __table_args__ = (
        UniqueConstraint("source_id", "category", name="ux_crawl_source_cat"),
    )

    id:        Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), nullable=False)
    category:  Mapped[str] = mapped_column(String(50), nullable=False)

    # Граница «свежести»: всё, что добавлено позже — ещё не обработано.
    # NULL = первый полный обход ещё не завершён.
    oldest_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Продолжение незавершённого первого обхода.
    # 0 = не идёт, >0 = следующая страница для обработки (снизу вверх).
    resume_page: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)

    last_page_max: Mapped[int | None] = mapped_column(Integer)
    last_run_at:   Mapped[datetime | None] = mapped_column(DateTime)

    source: Mapped[Source] = relationship()