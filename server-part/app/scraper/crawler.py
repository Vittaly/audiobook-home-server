"""
Обход страниц поиска.

Не знает ни про БД, ни про Book, ни про каталог.
Работает с CrawlState (границы, курсор) и отдаёт страницы по одной.
"""

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Callable

from app.config import settings
from app.db.models import CrawlState
from app.scraper import rutor

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, CrawlState], None]


async def walk(
    query: str,
    state: CrawlState,
    on_progress: ProgressFn,
) -> AsyncIterator[list[dict]]:
    """
    Отдаёт страницы раздач по одной.

    Первый обход (state.oldest_seen_at IS NULL) — снизу вверх,
    начиная с последней страницы.

    Последующие — сверху вниз, стоп на первой полностью старой странице.
    """
    if state.oldest_seen_at is None:
        async for page in _initial_scan(query, state, on_progress):
            yield page
    else:
        async for page in _incremental_scan(query, state, on_progress):
            yield page


async def _initial_scan(query, state, on_progress) -> AsyncIterator[list[dict]]:
    """Первый обход: последняя страница → 0."""
    if state.resume_page == 0:
        last_page = await _find_last_page(query)
        if last_page is None:
            log.warning("Не удалось найти последнюю страницу")
            return

        last_items = await rutor.fetch_search_page(last_page, query=query)
        if not last_items:
            return

        last_date = _parse_date(last_items[-1].get("added_at"))
        if last_date is None:
            log.warning("Не удалось распарсить дату последней записи")
            return

        state.oldest_seen_at = last_date
        state.resume_page = last_page
        state.last_page_max = last_page
        log.info("Граница: %s, старт со страницы %d",
                 last_date.date(), last_page)

    page = state.resume_page
    while page >= 0:
        if page < state.resume_page:
            await asyncio.sleep(settings.SCRAPE_DELAY_SEC)

        items = await rutor.fetch_search_page(page, query=query)
        if not items:
            break

        yield items

        page -= 1
        state.resume_page = page
        on_progress(page, state)


async def _incremental_scan(query, state, on_progress) -> AsyncIterator[list[dict]]:
    """Свежие страницы: 0 → ... → стоп на старой."""
    page = 0
    while True:
        if page > 0:
            await asyncio.sleep(settings.SCRAPE_DELAY_SEC)

        items = await rutor.fetch_search_page(page, query=query)
        if not items:
            break

        if not _has_fresh(items, state.oldest_seen_at):
            log.info("Страница %d вся старее %s, стоп",
                     page, state.oldest_seen_at.date())
            break

        yield items

        if len(items) < 100:
            break

        page += 1
        on_progress(page, state)


async def _find_last_page(query: str) -> int | None:
    for page in range(19, -1, -1):
        if page < 19:
            await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        items = await rutor.fetch_search_page(page, query=query)
        if items:
            return page
    return None


def _has_fresh(items: list[dict], oldest_seen: datetime) -> bool:
    for item in items:
        d = _parse_date(item.get("added_at"))
        if d is not None and d > oldest_seen:
            return True
    return False


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None