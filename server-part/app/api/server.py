"""
FastAPI приложение audiobook-server.

Собирает роуты, поднимает:
  - CoverService (кэш и скачивание обложек);
  - TorrentClientDaemon (автозапуск торрент-клиента по TORRENT_BACKEND);
  - ServiceAnnouncer (mDNS-анонс для автообнаружения);
  - AsyncIOScheduler (фоновые задачи).

Все шаги логируются с префиксом >>> для удобной фильтрации.
Access-логи HTTP-запросов пишет AccessLogMiddleware.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import AccessLogMiddleware
from app.catalog.covers import CoverService
from app.config import settings
from app.db.session import init_db
from app.discovery import ServiceAnnouncer
from app.scheduler.jobs import build_scheduler
from app.torrents.daemon import build_launcher

log = logging.getLogger(__name__)


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(">>> lifespan: вход")

    # ---------- каталоги ----------
    try:
        settings.ensure_dirs()
        log.info(">>> lifespan: каталоги готовы (%s)", settings.data_dir)
    except Exception:
        log.exception(">>> lifespan: ошибка ensure_dirs")
        raise

    # ---------- БД ----------
    try:
        init_db()
        log.info(">>> lifespan: БД инициализирована")
    except Exception:
        log.exception(">>> lifespan: ошибка init_db")
        raise

    # ---------- CoverService ----------
    try:
        covers = CoverService()
        app.state.covers = covers
        log.info(">>> lifespan: CoverService создан")
    except Exception:
        log.exception(">>> lifespan: ошибка CoverService")
        raise

    # ---------- торрент-клиент (автозапуск по TORRENT_BACKEND) ----------
    launcher = build_launcher()
    mode = launcher.__class__.__name__
    log.info(">>> lifespan: запускаем торрент-клиент (%s)", mode)
    try:
        ok = await launcher.start()
    except Exception as exc:
        # Исключение из start() — клиент точно не поднялся.
        log.critical(">>> lifespan: ошибка запуска торрент-клиента (%s): %s",
                     mode, exc, exc_info=True)
        raise RuntimeError(
            f"Не удалось запустить торрент-клиент ({mode}): {exc}"
        ) from exc

    if not ok:
        # start() вернул False: клиент не найден/упал/порт не открылся.
        # Без торрент-клиента сервис (скачивание) бесполезен —
        # завершаем работу с понятной ошибкой, а не работаем «вхолостую».
        log.critical(">>> lifespan: торрент-клиент (%s) не запустился. "
                     "Без него работа сервиса бессмысленна — останавливаемся.",
                     mode)
        raise RuntimeError(
            f"Торрент-клиент ({mode}) не запустился. Проверь .env "
            f"(TORRENT_BACKEND, пути к бинарю) и логи выше."
        )

    log.info(">>> lifespan: торрент-клиент (%s) готов", mode)
    app.state.torrent_client = launcher
    app.state.torrent_client_ok = True

    # ---------- mDNS ----------
    log.info(">>> lifespan: запускаем ServiceAnnouncer")
    announcer = ServiceAnnouncer()
    try:
        await announcer.start()
        log.info(">>> lifespan: ServiceAnnouncer запущен")
    except Exception:
        log.exception(">>> lifespan: ошибка ServiceAnnouncer.start")
    app.state.announcer = announcer

    # ---------- планировщик ----------
    try:
        scheduler = build_scheduler(covers)
        log.info(">>> lifespan: scheduler собран, state=%s, running=%s",
                 scheduler.state, scheduler.running)
        log.info(">>> lifespan: задачи: %s",
                 [(j.id, str(j.next_run_time)) for j in scheduler.get_jobs()])
    except Exception:
        log.exception(">>> lifespan: ошибка build_scheduler")
        raise

    try:
        scheduler.start()
        app.state.scheduler = scheduler
        log.info(">>> lifespan: scheduler.start() вызван, "
                 "state=%s, running=%s",
                 scheduler.state, scheduler.running)
    except Exception:
        log.exception(">>> lifespan: ошибка scheduler.start")
        raise

    log.info(">>> lifespan: startup завершён, отдаём управление")

    # ---------- работа ----------
    try:
        yield
    except Exception:
        log.exception(">>> lifespan: ошибка во время работы приложения")
        raise
    finally:
        log.info(">>> lifespan: shutdown начался")

        # планировщик
        try:
            scheduler.shutdown(wait=False)
            log.info(">>> lifespan: scheduler остановлен")
        except Exception:
            log.exception(">>> lifespan: ошибка scheduler.shutdown")

        # mDNS
        try:
            await announcer.stop()
            log.info(">>> lifespan: ServiceAnnouncer остановлен")
        except Exception:
            log.exception(">>> lifespan: ошибка announcer.stop")

        # обложки
        try:
            await covers.aclose()
            log.info(">>> lifespan: CoverService закрыт")
        except Exception:
            log.exception(">>> lifespan: ошибка covers.aclose")

        # торрент-клиент — только если мы его запускали
        try:
            await launcher.stop()
            log.info(">>> lifespan: торрент-клиент остановлен")
        except Exception:
            log.exception(">>> lifespan: ошибка launcher.stop")

        log.info(">>> lifespan: выход")


# ============================================================
# ФАБРИКА ПРИЛОЖЕНИЯ
# ============================================================

def create_app() -> FastAPI:
    log.info(">>> create_app: старт")

    # ---------- роуты ----------
    try:
        from app.api.routes import (
            books, covers, downloads, health, stream,
        )
        log.info(">>> create_app: роуты импортированы")
    except Exception:
        log.exception(">>> create_app: ошибка импорта роутов")
        raise

    # ---------- FastAPI ----------
    app = FastAPI(
        title="audiobook-server",
        version="0.1.0",
        lifespan=lifespan,
    )
    log.info(">>> create_app: FastAPI создан, lifespan=%s",
             app.router.lifespan_context)

    # ---------- middleware ----------
    # подключаем до include_router, чтобы все запросы логировались
    app.add_middleware(AccessLogMiddleware)
    log.info(">>> create_app: AccessLogMiddleware подключён")

    # ---------- роуты ----------
    app.include_router(health.router, tags=["system"])
    app.include_router(books.router, tags=["books"])
    app.include_router(covers.router, tags=["covers"])
    app.include_router(downloads.router, tags=["downloads"])
    app.include_router(stream.router, tags=["streaming"])

    log.info(">>> create_app: роуты подключены, всего маршрутов=%d",
             len(app.routes))
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            log.debug(">>> create_app:   %s %s",
                      ",".join(sorted(methods)), path)

    return app