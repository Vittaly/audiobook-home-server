from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.release import Release

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


class Rubric(Base):
    """Жанр / тег / рубрика. Рубрики живут на карточке раздачи."""
    __tablename__ = "rubric"
    __table_args__ = (
        UniqueConstraint("kind", "title_norm", name="ux_rubric_kind_title"),
        CheckConstraint("kind IN ('genre','tag','rubric')", name="ck_rubric_kind"),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    kind:       Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title:      Mapped[str] = mapped_column(String(255), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(255), nullable=False)

    releases: Mapped[list["ReleaseRubric"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan")

    # ---------------------------------------------------------------
    # CRUD
    # ---------------------------------------------------------------

    @classmethod
    def find_or_create(cls, db: Session, kind: str, title: str) -> "Rubric":
        """Находит рубрику по типу + нормализованному названию или создаёт.

        Args:
            db:    открытая сессия.
            kind:  'genre' | 'tag' | 'rubric'.
            title: название рубрики.

        Returns:
            Существующий или новый экземпляр Rubric (уже добавлен в сессию).
        """
        title_norm = _norm(title)
        rubric = db.execute(
            select(cls).where(
                cls.kind == kind,
                cls.title_norm == title_norm,
            )
        ).scalar_one_or_none()
        if rubric is None:
            rubric = cls(kind=kind, title=title, title_norm=title_norm)
            db.add(rubric)
            db.flush()
            log.info("rubric: создана новая %r / %s", title, kind)
        return rubric


class ReleaseRubric(Base):
    __tablename__ = "release_rubric"

    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"), primary_key=True)
    rubric_id:  Mapped[int] = mapped_column(
        ForeignKey("rubric.id", ondelete="CASCADE"), primary_key=True)

    release: Mapped["Release"] = relationship(back_populates="rubrics")
    rubric:  Mapped["Rubric"]  = relationship(back_populates="releases")
