"""
Поиск обложек на LiveLib через HTML-страницу поиска.

Не имеет открытого API, но страница поиска отдаёт HTML с прямыми
ссылками на обложки. Парсим через selectolax.

Хрупкость: средняя — при смене вёрстки сломается.
Ограничение: только для классики и переводных изданий,
современные аудиокниги плохо представлены.
"""
from __future__ import annotations


import logging
from urllib.parse import quote, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger(__name__)

_BASE = "https://www.livelib.ru"
_SEARCH_URL = f"{_BASE}/find/{{query}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def search_cover(title: str, author: str,
                       timeout: float = 10.0) -> str | None:
    if not title:
        return None

    query = f"{author} {title}".strip()
    url = _SEARCH_URL.format(query=quote(query))

    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=_HEADERS, follow_redirects=True,
        ) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        log.warning("LiveLib: сеть — %s", e)
        return None

    if r.status_code != 200:
        log.warning("LiveLib: HTTP %d", r.status_code)
        return None

    return _parse_search_page(r.text, title)


def _parse_search_page(html: str, title: str) -> str | None:
    """
    Ищет первый блок книги с обложкой.

    LiveLib отдаёт разметку с <img class="bookcover"> или подобную.
    Селектор может меняться — при поломке обновить.
    """
    tree = LexborHTMLParser(html)

    # карточки книг на странице поиска
    for card in tree.css(".book-item, .bookcard, [class*='book-cover']"):
        # ищем картинку
        img = card.css_first("img")
        if img is None:
            continue

        src = img.attributes.get("src") or img.attributes.get("data-src")
        if not src:
            continue

        # фильтруем заглушки и иконки
        if "no-cover" in src or "placeholder" in src:
            continue
        if src.endswith((".gif", ".svg")):
            continue

        return urljoin(_BASE, src)

    return None