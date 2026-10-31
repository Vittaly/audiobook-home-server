import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.models.book import Book
from app.db.models.chapter import Chapter
from app.db.models.release import Release

log = logging.getLogger(__name__)


@event.listens_for(Session, "before_flush")
def _recompute_dirty_statuses(session: Session, flush_context, instances):
    """
    Если кто-то поменял Chapter.status или Book.status напрямую (в обход
    set_status/recompute_status), перед flush находим затронутые книги и
    карточки-раздачи и пересчитываем их статусы.
    Работает как страховка, не как основной механизм.
    """
    touched_books: set[str] = set()
    touched_releases: set[int] = set()

    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Chapter) and obj.book_hash is not None:
            touched_books.add(obj.book_hash)
        elif isinstance(obj, Book):
            touched_books.add(obj.book_hash)
            for release in obj.releases:
                touched_releases.add(release.id)

    for book_hash in touched_books:
        book = session.get(Book, book_hash)
        if book is not None:
            book.recompute_status()

    for release_id in touched_releases:
        release = session.get(Release, release_id)
        if release is not None:
            release.recompute_status()
