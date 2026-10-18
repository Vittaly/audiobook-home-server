import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import models  # noqa: F401  — регистрация таблиц в Base.metadata
from app.db.base import Base
from app.db import events  # noqa: F401  — активация before_flush

log = logging.getLogger(__name__)

engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)


@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    # По умолчанию в Python sqlite3 busy_timeout = 5с: если фоновый
    # поллер/ingest в этот момент держит write-лок, запрос из API падает
    # с "database is locked". Ждём лок до 30с вместо падения.
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, class_=Session,
)


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Откат транзакции")
        raise
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_legacy_schema() -> None:
    """Добавляет колонки, отсутствующие в старых SQLite-базах.

    create_all создаёт только новые таблицы и НЕ добавляет колонки к уже
    существующим — поэтому нужные колонки добавляем вручную через
    PRAGMA table_info + ALTER TABLE.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(book)"))
        }
        if "is_provisional" not in cols:
            conn.execute(text(
                "ALTER TABLE book ADD COLUMN is_provisional "
                "INTEGER NOT NULL DEFAULT 0"
            ))
            log.info("Миграция: добавлена колонка book.is_provisional")

        # release.released_at — дата создания раздачи на трекере.
        # SQLite не умеет ADD COLUMN NOT NULL без константного DEFAULT,
        # поэтому добавляем обычной колонкой и сразу бэкфиллим её значением
        # added_at (для старых записей порядок сохранится; при следующем
        # обходе crawler перезапишет реальной датой трекера).
        rel_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(release)"))
        }
        if "released_at" not in rel_cols:
            conn.execute(text("ALTER TABLE release ADD COLUMN released_at DATETIME"))
            conn.execute(text(
                "UPDATE release SET released_at = added_at "
                "WHERE released_at IS NULL"
            ))
            log.info("Миграция: добавлена колонка release.released_at")


def init_db() -> None:
    settings.ensure_dirs()
    Base.metadata.create_all(engine)
    _migrate_legacy_schema()
    log.info("БД инициализирована: %s", settings.db_file)