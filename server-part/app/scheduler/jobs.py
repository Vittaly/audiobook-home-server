"""
Фоновые задачи планировщика.

Две задачи:
  - sync_catalog   — обход rutor.info, наполнение каталога;
  - poll_downloads — синхронизация состояния торрентов с БД
                     (создание глав, обновление прогресса,
                     обработка удалённых торрентов).

Каждая задача — экземпляр Job (app.scheduler.base).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.catalog.covers import CoverService
from app.catalog.ingest import CatalogScanJob
from app.config import settings
from app.scraper.rutor import RutorSource
from app.torrents.poller import poll_downloads

log = logging.getLogger(__name__)


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

    scan_job = CatalogScanJob(RutorSource(), covers)

    scheduler.add_job(
        scan_job.execute,
        trigger=IntervalTrigger(hours=settings.SCRAPE_INTERVAL_HOURS),
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
        poll_downloads,
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