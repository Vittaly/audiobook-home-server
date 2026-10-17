"""
Поиск обложек книг в открытых источниках.

Порядок источников для русскоязычных книг:
    1. ЛитРес        — лучшее покрытие русских аудиокниг
    2. LiveLib       — классика, переводные издания
    3. Google Books  — langRestrict=ru
    4. Open Library  — старые издания
    5. iTunes        — country=ru
    6. Audnexus      — только для латиницы (зарубежные книги)

Fantlab подключается отдельно для жанровой фантастики,
если book.rubrics содержит «фантастика» / «фэнтези».
"""

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class CoverSearchResult:
    url: str
    source: str


async def search_cover(title: str, author: str,
                       rubrics: list[str] | None = None) -> CoverSearchResult | None:
    """
    Каскадный поиск обложки. Возвращает первый успешный результат.
    rubrics — список тегов книги, влияет на подключение Fantlab.
    """
    if not title:
        return None

    rubrics_lower = {r.lower() for r in (rubrics or [])}

    # --- 1. ЛитРес ---
    url = await _try_litres(title, author)
    if url:
        return CoverSearchResult(url, "litres")

    # --- 2. LiveLib ---
    url = await _try_livelib(title, author)
    if url:
        return CoverSearchResult(url, "livelib")

    # --- 3. Google Books (ru) ---
    url = await _try_google_books(title, author, lang="ru")
    if url:
        return CoverSearchResult(url, "google_books")

    # --- 4. Open Library ---
    url = await _try_open_library(title, author)
    if url:
        return CoverSearchResult(url, "open_library")

    # --- 5. iTunes (ru) ---
    url = await _try_itunes(title, author, country="ru")
    if url:
        return CoverSearchResult(url, "itunes")

    # --- 6. Fantlab — только для жанровой фантастики ---
    if _is_fantasy(rubrics_lower):
        url = await _try_fantlab(title, author)
        if url:
            return CoverSearchResult(url, "fantlab")

    # --- 7. Audnexus — только для латиницы ---
    if _is_latin(title):
        url = await _try_audnexus(title, author)
        if url:
            return CoverSearchResult(url, "audnexus")

    log.info("Обложка не найдена: author=%r, title=%r", author, title)
    return None


# ============================================================
# ОБЁРТКИ ИСТОЧНИКОВ
# ============================================================

async def _try_litres(title, author) -> str | None:
    try:
        from app.catalog import litres_search
        return await litres_search.search_cover(
            title, author, timeout=settings.COVER_SEARCH_TIMEOUT)
    except Exception:
        log.exception("ЛитРес: неожиданная ошибка")
        return None


async def _try_livelib(title, author) -> str | None:
    try:
        from app.catalog import livelib_search
        return await livelib_search.search_cover(
            title, author, timeout=settings.COVER_SEARCH_TIMEOUT)
    except Exception:
        log.exception("LiveLib: неожиданная ошибка")
        return None


async def _try_fantlab(title, author) -> str | None:
    try:
        from app.catalog import fantlab_search
        return await fantlab_search.search_cover(
            title, author, timeout=settings.COVER_SEARCH_TIMEOUT)
    except Exception:
        log.exception("Fantlab: неожиданная ошибка")
        return None


async def _try_google_books(title, author, lang="ru") -> str | None:
    query = f'intitle:"{title}"'
    if author:
        query += f'+inauthor:"{author}"'

    params = {"q": query, "maxResults": 5}
    if lang:
        params["langRestrict"] = lang

    try:
        async with httpx.AsyncClient(
            timeout=settings.COVER_SEARCH_TIMEOUT,
            follow_redirects=True,
        ) as client:
            r = await client.get(
                "https://www.googleapis.com/books/v1/volumes", params=params)
    except httpx.HTTPError:
        return None

    if r.status_code != 200:
        return None

    for item in r.json().get("items", []) or []:
        links = (item.get("volumeInfo") or {}).get("imageLinks") or {}
        cover = links.get("thumbnail") or links.get("smallThumbnail")
        if cover:
            return (cover.replace("&zoom=1", "&zoom=2")
                         .replace("http://", "https://"))
    return None


async def _try_open_library(title, author) -> str | None:
    q = f"{title} {author}".strip()
    try:
        async with httpx.AsyncClient(
            timeout=settings.COVER_SEARCH_TIMEOUT,
        ) as client:
            r = await client.get(
                "https://openlibrary.org/search.json",
                params={"q": q, "limit": 5},
            )
    except httpx.HTTPError:
        return None

    if r.status_code != 200:
        return None

    for doc in r.json().get("docs", []) or []:
        cover_id = doc.get("cover_i")
        if cover_id:
            return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    return None


async def _try_itunes(title, author, country="ru") -> str | None:
    term = f"{author} {title}".strip()
    try:
        async with httpx.AsyncClient(
            timeout=settings.COVER_SEARCH_TIMEOUT,
        ) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={
                    "term": term,
                    "media": "audiobook",
                    "entity": "audiobook",
                    "country": country,
                    "limit": 5,
                },
            )
    except httpx.HTTPError:
        return None

    if r.status_code != 200:
        return None

    for item in r.json().get("results", []) or []:
        art = item.get("artworkUrl100")
        if art:
            return art.replace("100x100", "600x600")
    return None


async def _try_audnexus(title, author) -> str | None:
    if not author:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=settings.COVER_SEARCH_TIMEOUT,
            headers={"User-Agent": "audiobook-server/1.0"},
        ) as client:
            r = await client.get(
                "https://api.audnex.us/authors",
                params={"name": author},
            )
            if r.status_code != 200:
                return None
            authors = r.json()
            if not authors:
                return None
            asin = authors[0].get("asin")
            if not asin:
                return None

            r = await client.get(
                f"https://api.audnex.us/authors/{asin}/books")
            if r.status_code != 200:
                return None
            for b in r.json():
                img = b.get("image")
                if img:
                    return img
    except (httpx.HTTPError, ValueError):
        return None
    return None


# ============================================================
# ХЕЛПЕРЫ
# ============================================================

def _is_fantasy(rubrics: set[str]) -> bool:
    markers = ("фантастика", "фэнтези", "фантастик", "sci-fi")
    return any(m in r for r in rubrics for m in markers)


def _is_latin(text: str) -> bool:
    """Текст преимущественно латиницей."""
    if not text:
        return False
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return latin > len(text) * 0.5