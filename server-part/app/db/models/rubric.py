from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.release import Release


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


class ReleaseRubric(Base):
    __tablename__ = "release_rubric"

    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), primary_key=True)
    rubric_id:  Mapped[int] = mapped_column(
        ForeignKey("rubric.id", ondelete="CASCADE"), primary_key=True)

    release: Mapped["Release"] = relationship(back_populates="rubrics")
    rubric:  Mapped["Rubric"]  = relationship(back_populates="releases")
