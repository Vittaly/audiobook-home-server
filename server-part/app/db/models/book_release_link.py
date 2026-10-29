"""
Промежуточная таблица для связи M:N Book ↔ Release.
Определяется как отдельная Table (не модель-класс), чтобы использовать
в качестве secondary в отношениях Book.releases и Release.books.
"""
from sqlalchemy import Column, ForeignKey, Integer, String, Table

from app.db.base import Base

book_release_link = Table(
    "book_release_link",
    Base.metadata,
    Column("book_hash", String(64),
           ForeignKey("books.book_hash", ondelete="CASCADE"),
           primary_key=True),
    Column("release_id", Integer,
           ForeignKey("releases.id", ondelete="CASCADE"),
           primary_key=True),
)