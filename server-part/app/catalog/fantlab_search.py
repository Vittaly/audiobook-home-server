"""
Поиск обложек на Fantlab.

Использует открытый эндпоинт api.fantlab.ru для поиска произведений.
Лучше всего работает для фантастики, фэнтези, научной фантастики.
Для остальных жанров почти бесполезен — не подключать в общий каскад.
"""

import logging

import httpx

log = logging.getLogger(__name__)

_SEARCH_URL = "https://api.fantlab.ru/search-autocomplete"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://fantlab.ru/",
}


async def search_cover(title: str, author: str,
                       timeout: float = 10.0) -> str | None:
    if not title:
        return None

    query = f"{author} {title}".strip()

    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=_HEADERS, follow_redirects=True,
        ) as client:
            r = await client.get(_SEARCH_URL, params={"q": query})
    except httpx.HTTPError as e:
        log.warning("Fantlab: сеть — %s", e)
        return None

    if r.status_code != 200:
        log.warning("Fantlab: HTTP %d", r.status_code)
        return None

    try:
        data = r.json()
    except ValueError:
        return None

    # Fantlab возвращает список работ с полем image
    items = data if isinstance(data, list) else data.get("works", [])
    for item in items:
        image = item.get("image")
        if image:
            if image.startswith("//"):
                image = "https:" + image
            return image

    return None