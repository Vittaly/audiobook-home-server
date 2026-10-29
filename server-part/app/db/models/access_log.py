from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccessLog(Base):
    __tablename__ = "access_log"
    __table_args__ = (
        CheckConstraint(
            "action IN ('list','detail','stream','download','delete')",
            name="ck_access_action",
        ),
    )

    id:          Mapped[int] = mapped_column(Integer, primary_key=True)
    book_hash:   Mapped[str] = mapped_column(
        ForeignKey("books.book_hash", ondelete="CASCADE"), nullable=False, index=True)
    chapter_id:  Mapped[int | None] = mapped_column(
        ForeignKey("chapter.id", ondelete="SET NULL"))
    action:      Mapped[str] = mapped_column(String(20), nullable=False)
    client_ip:   Mapped[str | None] = mapped_column(String(45))
    user_agent:  Mapped[str | None] = mapped_column(String(255))
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)