"""
Фоновые задачи планировщика.

Две задачи:
  - sync_catalog   — обход rutor.info, наполнение каталога;
  - poll_downloads — синхронизация состояния торрентов с БД
                     (создание глав, обновление прогресса,
                     обработка удалённых торрентов).

Каждая задача обёрнута в _logged: логирует старт, финиш, длительность
и полный трейс при ошибке. Исключение в задаче не убивает планировщик.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.catalog.covers import CoverService
from app.catalog.ingest import sync_catalog
from app.config import settings
from app.torrents.poller import poll_downloads

log = logging.getLogger(__name__)


# ============================================================
# ОБЁРТКА ЗАДАЧ
# ============================================================

def _logged(name):
    """
    Декоратор: логирует старт/финиш/ошибку задачи и её длительность.
    Не даёт исключению убить планировщик.
    """
    def wrap(fn):
        async def inner(*args, **kwargs):
            t0 = datetime.now(timezone.utc)
            log.info(">>> task %s: СТАРТ", name)
            try:
                result = await fn(*args, **kwargs)
                dt = (datetime.now(timezone.utc) - t0).total_seconds()
                log.info(">>> task %s: ГОТОВО (%s) за %.1f сек",
                         name, result, dt)
                return result
            except Exception:
                dt = (datetime.now(timezone.utc) - t0).total_seconds()
                log.exception(">>> task %s: ОШИБКА за %.1f сек", name, dt)
        return inner
    return wrap


# ============================================================
# СБОРКА ПЛАНИРОВЩИКА
# ============================================================

def build_scheduler(covers: CoverService) -> AsyncIOScheduler:
    log.info(">>> build_scheduler: вход")
    log.info(">>> build_scheduler: SCRAPE_INTERVAL_HOURS=%d",
             settings.SCRAPE_INTERVAL_HOURS)
    log.info(">>> build_scheduler: SCRAPE_RUN_ON_START=%s",
             settings.SCRAPE_RUN_ON_START)
    log.info(">>> build_scheduler: TRANSMISSION_POLL_SEC=%d",
             settings.TRANSMISSION_POLL_SEC)

    scheduler = AsyncIOScheduler(timezone="UTC")
    log.info(">>> build_scheduler: scheduler создан")

    # ----------------------------------------------------------
    # 1. Сканирование каталога
    # ----------------------------------------------------------
    if settings.SCRAPE_RUN_ON_START:
        scrape_first_run = datetime.now(timezone.utc) + timedelta(seconds=5)
        log.info(">>> build_scheduler: первый запуск sync_catalog в %s",
                 scrape_first_run)
    else:
        scrape_first_run = None
        log.info(">>> build_scheduler: sync_catalog стартует по триггеру")

    scheduler.add_job(
        _logged("sync_catalog")(sync_catalog),
        trigger=IntervalTrigger(hours=settings.SCRAPE_INTERVAL_HOURS),
        args=[covers],
        id="sync_catalog",
        max_instances=1,
        coalesce=True,
        next_run_time=scrape_first_run,
    )
    log.info(">>> build_scheduler: job sync_catalog добавлен "
             "(interval=%d ч)", settings.SCRAPE_INTERVAL_HOURS)

    # ----------------------------------------------------------
    # 2. Синхронизация торрентов с БД
    # ----------------------------------------------------------
    poll_first_run = datetime.now(timezone.utc) + timedelta(seconds=10)
    log.info(">>> build_scheduler: первый запуск poll_downloads в %s",
             poll_first_run)

    scheduler.add_job(
        _logged("poll_downloads")(poll_downloads),
        trigger=IntervalTrigger(seconds=settings.TRANSMISSION_POLL_SEC),
        args=[covers],
        id="poll_downloads",
        max_instances=1,
        coalesce=True,
        next_run_time=poll_first_run,
    )
    log.info(">>> build_scheduler: job poll_downloads добавлен "
             "(interval=%d сек)", settings.TRANSMISSION_POLL_SEC)

    # ----------------------------------------------------------
    # Итог
    # ----------------------------------------------------------
    jobs = scheduler.get_jobs()
    log.info(">>> build_scheduler: всего задач %d", len(jobs))
    for j in jobs:
        log.info(">>>   id=%s trigger=%s next_run=%s",
                 j.id, j.trigger, getattr(j, "next_run_time", None))

    log.info(">>> build_scheduler: выход")
    return scheduler