from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.release import Release


class Person(Base):
    """Автор, чтец, переводчик и т.п. Персоны живут на карточке раздачи."""
    __tablename__ = "person"

    id:        Mapped[int] = mapped_column(Integer, primary_key=True)
    name:      Mapped[str] = mapped_column(String(255), nullable=False)
    name_norm: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True)

    releases: Mapped[list["ReleasePerson"]] = relationship(
        back_populates="person", cascade="all, delete-orphan")


class ReleasePerson(Base):
    __tablename__ = "release_person"
    __table_args__ = (
        CheckConstraint(
            "role IN ('author','narrator','translator','editor','illustrator')",
            name="ck_release_person_role",
        ),
    )

    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), primary_key=True)
    person_id:  Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), primary_key=True)
    role:       Mapped[str] = mapped_column(String(20), primary_key=True)
    ordinal:    Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    release: Mapped["Release"] = relationship(back_populates="persons")
    person:  Mapped["Person"]  = relationship(back_populates="releases")
