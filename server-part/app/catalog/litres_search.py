"""
Поиск обложек на ЛитРес.

ЛитРес — Next.js с SSR: результаты поиска и ссылки на обложки
присутствуют прямо в HTML страницы. Не нужен ни __NEXT_DATA__,
ни внутренний API.

Селекторы (проверены на живой странице):
  Карточка книги:  [data-testid="art__wrapper"]
  Название:        [data-testid="art__title"]
  Автор:           [data-testid="art__authorName--link"]
  Обложка:         [data-testid="art__cover"] img[src]

Обложки лежат по URL без расширения:
  https://cdn.litres.ru/pub/c/cover_415/{art_id}
"""
from __future__ import annotations


import logging
from urllib.parse import quote

import httpx
from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.litres.ru/search/"

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
    """
    Ищет обложку на странице поиска ЛитРеса.
    Возвращает URL картинки или None.
    """
    if not title:
        return None

    query = f"{author} {title}".strip()
    url = f"{_SEARCH_URL}?q={quote(query)}"

    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=_HEADERS, follow_redirects=True,
        ) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        log.warning("ЛитРес: сеть — %s", e)
        return None

    if r.status_code != 200:
        log.warning("ЛитРес: HTTP %d", r.status_code)
        return None

    items = _parse_results(r.text)
    if not items:
        log.info("ЛитРес: результатов нет для %r", query)
        return None

    log.debug("ЛитРес: найдено %d результатов для %r", len(items), query)

    best = _pick_best(items, title, author)
    return best["cover"] if best else None


# ============================================================
# ПАРСИНГ
# ============================================================

def _parse_results(html: str) -> list[dict]:
    """
    Возвращает список карточек из HTML страницы поиска.
    Каждая карточка — {title, author, cover}.
    """
    tree = LexborHTMLParser(html)
    results: list[dict] = []

    for card in tree.css('[data-testid="art__wrapper"]'):
        title_node = card.css_first('[data-testid="art__title"]')
        author_node = card.css_first('[data-testid="art__authorName--link"]')
        cover_img = card.css_first('[data-testid="art__cover"] img')

        if title_node is None or cover_img is None:
            continue

        cover_src = cover_img.attributes.get("src") or ""
        if not cover_src:
            continue

        # отсеиваем placeholder-иконки
        if "cover_415" not in cover_src:
            continue

        results.append({
            "title":  _text(title_node),
            "author": _text(author_node),
            "cover":  _normalize_cover_url(cover_src),
        })

    return results


def _text(node) -> str:
    if node is None:
        return ""
    return " ".join(node.text(strip=True).split())


def _normalize_cover_url(src: str) -> str:
    """
    ЛитРес отдаёт URL без расширения:
      https://cdn.litres.ru/pub/c/cover_415/74246531
    Оставляем как есть — CDN сам отдаёт нужный Content-Type.
    На всякий случай убираем параметры ?...
    """
    return src.split("?")[0]


# ============================================================
# ВЫБОР ЛУЧШЕГО СОВПАДЕНИЯ
# ============================================================

def _pick_best(items: list[dict], title: str, author: str) -> dict | None:
    title_lower = title.lower().strip()
    author_lower = author.lower().strip() if author else ""

    # 1. точное совпадение названия + автор
    for it in items:
        if _norm(it["title"]) == title_lower and \
           _author_matches(it["author"], author_lower):
            return it

    # 2. название целиком входит в результат (или наоборот) + автор
    for it in items:
        t = _norm(it["title"])
        if (title_lower in t or t in title_lower) and \
           _author_matches(it["author"], author_lower):
            return it

    # 3. только название
    for it in items:
        t = _norm(it["title"])
        if title_lower in t or t in title_lower:
            return it

    # 4. первый результат (ЛитРес сортирует по релевантности)
    return items[0] if items else None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _author_matches(author_in_item: str, author_lower: str) -> bool:
    if not author_lower:
        return True
    a = _norm(author_in_item)
    return author_lower in a or a in author_lower