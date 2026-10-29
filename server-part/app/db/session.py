"""
Управление сессиями SQLAlchemy.

Архитектура:
  engine          — глобальный (один на процесс), создаётся на старте.
  SessionManager  — фабрика сессий; в простейшем случае обёртка над
                    sessionmaker, но при необходимости может реализовать
                    пул, тред-локальные сессии и т.д.
  session_scope   — контекстный менеджер: открыть — commit — rollback — close.
  get_db          — generator для FastAPI Depends().

Потокобезопасность:
  Каждый вызов SessionManager.get() / session_scope() / get_db()
  создаёт НОВУЮ сессию. SQLAlchemy sessionmaker НЕ разделяет сессии
  между потоками, поэтому несколько потоков могут одновременно
  вызывать get() без взаимных блокировок (deadlock возможен только на
  уровне SQLite, что решается busy_timeout).
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import models  # noqa: F401  — регистрация таблиц в Base.metadata
from app.db.base import Base
from app.db import events  # noqa: F401  — активация before_flush

log = logging.getLogger(__name__)

# ===================================================================
# ДВИЖОК (один на процесс)
# ===================================================================

engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
    },
)


@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "Не удалось включить WAL: БД занята другим процессом. "
            "Проверьте, не запущен ли ВТОРОЙ экземпляр сервера "
            f"Исходная ошибка: {exc}"
        ) from exc
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


# ===================================================================
# МЕНЕДЖЕР СЕССИЙ
# ===================================================================


class SessionManager:
    """Фабрика сессий SQLAlchemy.

    Использование в FastAPI::

        sm = SessionManager()

        @app.get("/books")
        def list_books(db: Session = Depends(sm.get_depends)):
            ...

    Использование в коде::

        sm = SessionManager()
        db = sm.get()
        try:
            ...
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    Можно использовать как контекстный менеджер::

        with sm() as db:
            ...
    """

    def __init__(self) -> None:
        self._maker = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    # ---------------------------------------------------------------
    # Основной API
    # ---------------------------------------------------------------

    def get(self) -> Session:
        """Создаёт и возвращает НОВУЮ сессию.

        Вызывающий ОБЯЗАН гарантировать закрытие:
          db.close() — в finally-блоке.
        """
        return self._maker()

    def __call__(self) -> Session:
        """Короткий синоним self.get()."""
        return self.get()

    # ---------------------------------------------------------------
    # FastAPI Depends
    # ---------------------------------------------------------------

    def get_depends(self) -> Iterator[Session]:
        """Generator — можно передавать как Depends(get_depends)."""
        db = self.get()
        try:
            yield db
        finally:
            db.close()

    # ---------------------------------------------------------------
    # Context manager
    # ---------------------------------------------------------------

    @contextmanager
    def scope(self) -> Iterator[Session]:
        """Контекстный менеджер: commit при успехе, rollback при ошибке.

        Usage::

            with sm.scope() as db:
                db.add(...)
        """
        db = self.get()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            log.exception("SessionManager: откат транзакции")
            raise
        finally:
            db.close()


# ===================================================================
# SINGLETON — глобальный менеджер
# ===================================================================

session_manager = SessionManager()

# Короткие алиасы для обратной совместимости
SessionLocal = session_manager  # SessionLocal() → session_manager.get()

# Для кода, который использует глобальный singleton напрямую.
# В будущем предпочтительнее инжектить SessionManager через Dependency Injection.
get_db = session_manager.get_depends


@contextmanager
def session_scope():
    """Обратно-совместимая обёртка."""
    with session_manager.scope() as db:
        yield db


# ===================================================================
# МИГРАЦИИ СХЕМЫ
# ===================================================================


def migrate_legacy_schema() -> None:
    """Добавляет колонки, отсутствующие в старых SQLite-базах.

    Вызывается однократно при старте сервера (main.py).
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

        # series table может отсутствовать в старой схеме
        tbls = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "series" not in tbls:
            log.info("Миграция: таблица series будет создана Base.metadata.create_all")


def init_db() -> None:
    """Создаёт таблицы БД и запускает миграции схемы.

    Вызывается однократно при старте сервера.
    """
    Base.metadata.create_all(bind=engine)
    migrate_legacy_schema()
    log.info("БД инициализирована")