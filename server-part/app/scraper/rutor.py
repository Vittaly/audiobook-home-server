"""
Парсер rutor.info.

Возвращает нормализованные данные:
  - fetch_search_page   → страница поиска, items готовы к записи в БД
  - fetch_torrent_details → детальная страница, поля готовы к применению
  - fetch_file_list     → список файлов
"""
from __future__ import annotations


import logging
import re
from urllib.parse import quote, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser

from app.config import settings
from app.scraper.normalize import (
    clean_title, detect_content_type, extract_author,
    extract_narrator, extract_year,
)

log = logging.getLogger(__name__)

BASE = settings.RUTOR_BASE_URL

CATEGORY_BOOKS = 11
SEARCH_METHOD_ALL_WORDS = 1
SEARCH_IN_TITLE = 0
SORT_BY_DATE = 0
SORT_DESC = 0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:([0-9a-fA-F]{40})")
_SIZE_RE = re.compile(r"([\d.,]+)\s*(TB|GB|MB|KB|B)", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{1,2})\s+([А-Яа-я]{3})\s+(\d{2})")
_DURATION_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
_FULL_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
_SIZE_BYTES_RE = re.compile(r"\((\d+)\s*Bytes?\)", re.IGNORECASE)

_LABELED_FIELD_RE = re.compile(
    r"<b>(?P<key>[^<]+)</b>\s*:\s*(?P<val>.*?)(?=<br\s*/?>|</td>|<b>)",
    re.DOTALL | re.IGNORECASE,
)
_DESC_RE = re.compile(
    r"<b>Описание</b>\s*:\s*(.*?)(?=<br\s*/?>\s*<b>|</td>|$)",
    re.DOTALL | re.IGNORECASE,
)

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_SIZE_MULT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


# ============================================================
# ОБХОД СТРАНИЦ
# ============================================================

def _build_search_url(query: str, page: int) -> str:
    return (
        f"{BASE}/search/{page}/{CATEGORY_BOOKS}/"
        f"{SEARCH_METHOD_ALL_WORDS}{SEARCH_IN_TITLE}/"
        f"{SORT_BY_DATE + SORT_DESC}/{query}"
    )


async def fetch_search_page(
    page: int,
    query: str = "mp3",
    http_timeout: float | None = None,
) -> list[dict] | None:
    """
    Одна страница поиска. Items нормализованы.
    Возвращает None при ошибке сети, [] при пустой странице.
    """
    timeout = http_timeout or settings.SCRAPE_HTTP_TIMEOUT
    url = _build_search_url(query, page)

    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, follow_redirects=True,
    ) as client:
        log.info("GET %s", url)
        try:
            r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Ошибка на странице %d: %s", page, e)
            return None

    items = _parse_search_page(r.text, base=BASE)
    return [_normalize_search_item(it) for it in items]


def _parse_search_page(html: str, base: str = BASE) -> list[dict]:
    tree = LexborHTMLParser(html)
    items: list[dict] = []

    for row in tree.css("tr.gai, tr.tum"):
        item = _parse_row(row, base)
        if item:
            items.append(item)

    return items


def _normalize_search_item(item: dict) -> dict:
    """Приводит сырую запись к готовому виду."""
    title_raw = item.get("title_raw")
    title = clean_title(title_raw) or "Без названия"
    content_type, books_hint = detect_content_type(title_raw)

    return {
        "torrent_hash": item["torrent_hash"],
        "magnet":       item["magnet"],
        "source_url":   item.get("source_url"),
        "size_bytes":   item.get("size_bytes"),
        "seeds":        item.get("seeds"),
        "leeches":      item.get("leeches"),
        "added_at":     item.get("added_at"),
        "title_raw":    title_raw,
        # нормализованные
        "title":        title,
        "author":       extract_author(title_raw),
        "narrator":     extract_narrator(title_raw),
        "year":         extract_year(title_raw),
        "content_type": content_type,
        "books_hint":   books_hint,
    }


# ============================================================
# ДЕТАЛЬНАЯ СТРАНИЦА
# ============================================================

async def fetch_torrent_details(
    url_or_id: str | int,
    http_timeout: float | None = None,
) -> dict | None:
    """
    Детальная страница. Поля нормализованы: title, author, narrator,
    year, rubrics, description — готовы к применению к Book.
    """
    timeout = http_timeout or settings.SCRAPE_HTTP_TIMEOUT
    url = _build_torrent_url(url_or_id)

    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, follow_redirects=True,
    ) as client:
        log.info("GET %s", url)
        try:
            r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Не удалось получить %s: %s", url, e)
            return None

    raw = _parse_torrent_page(r.text, base=BASE, source_url=url)
    if raw is None:
        return None
    return _normalize_details(raw)


def _build_torrent_url(url_or_id: str | int) -> str:
    if isinstance(url_or_id, int) or str(url_or_id).isdigit():
        return f"{BASE}/torrent/{url_or_id}"
    return str(url_or_id)


def _parse_torrent_page(html: str, base: str = BASE,
                        source_url: str | None = None) -> dict | None:
    tree = LexborHTMLParser(html)
    details = tree.css_first("#details")
    if details is None:
        return None

    h1 = tree.css_first("h1")
    title_raw = _text(h1) if h1 else None

    magnet_a = details.css_first('a[href^="magnet:"]')
    if magnet_a is None:
        return None

    magnet = magnet_a.attributes.get("href", "")
    m = _MAGNET_RE.search(magnet)
    if not m:
        return None
    torrent_hash = m.group(1).lower()

    torrent_id = None
    dl_a = details.css_first('a[href*="/download/"]')
    if dl_a:
        dm = re.search(r"/download/(\d+)", dl_a.attributes.get("href", ""))
        if dm:
            torrent_id = int(dm.group(1))
    if torrent_id is None and source_url:
        sm = re.search(r"/torrent/(\d+)", source_url)
        if sm:
            torrent_id = int(sm.group(1))

    cover_url = _extract_cover(details, base)
    fields = _extract_labeled_fields(html)
    rubrics = [_text(a) for a in details.css('a[href^="/tag/"]')]
    related = _parse_related(details, base)

    return {
        "torrent_id":   torrent_id,
        "torrent_hash": torrent_hash,
        "magnet":       magnet,
        "title_raw":    title_raw,
        "cover_url":    cover_url,
        "source_url":   source_url or (
            f"{base}/torrent/{torrent_id}" if torrent_id else None),
        "fields":       fields,
        "rubrics_raw":  rubrics,
        "related_raw":  related,
    }


def _normalize_details(raw: dict) -> dict:
    """
    Превращает сырые поля детальной страницы в готовые к применению.

    title и author берём из title_raw (заголовок раздачи), а не из
    полей формы: заливщики часто путают «Название» и «Автор» местами
    или оставляют их пустыми. Заголовок формируется по имени торрента
    и надёжнее.

    Поля формы используем только для того, чего нет в заголовке:
    описание, жанры, чтец, обложка, издательство.
    """
    fields = raw.get("fields") or {}
    title_raw = raw.get("title_raw")

    # title и author — только из заголовка
    title = clean_title(title_raw)
    author = extract_author(title_raw)

    # чтец — отдельное поле, его не путают с названием
    narrator = (fields.get("Чтец")
                or fields.get("Исполнитель")
                or fields.get("Озвучивает")
                or extract_narrator(title_raw))

    # год — из формы, fallback на заголовок
    year = _first_int(fields.get("Год")) or extract_year(title_raw)

    # content_type — по заголовку (там есть «[5 книг]» и т.п.)
    content_type, books_hint = detect_content_type(title_raw)

    return {
        "torrent_id":   raw["torrent_id"],
        "torrent_hash": raw["torrent_hash"],
        "magnet":       raw["magnet"],
        "source_url":   raw.get("source_url"),

        "title":        title,
        "title_raw":    title_raw,
        "author":       author,
        "narrator":     narrator,
        "year":         year,
        "description":  fields.get("Описание"),
        "publisher":    fields.get("Издательство"),
        "format":       fields.get("Формат"),
        "duration_sec": _parse_duration(fields.get("Продолжительность")),
        "size_bytes":   _parse_size_bytes(fields.get("Размер")),
        "rubrics":      list(raw.get("rubrics_raw") or []),
        "cover_url":    raw.get("cover_url"),
        "content_type": content_type,
        "books_hint":   books_hint,
        "related":      raw.get("related_raw") or [],
    }


# ============================================================
# ФАЙЛ-ЛИСТ
# ============================================================

async def fetch_file_list(
    torrent_id: int,
    http_timeout: float | None = None,
) -> list[dict]:
    timeout = http_timeout or settings.SCRAPE_HTTP_TIMEOUT
    url = f"{BASE}/descriptions/{torrent_id}.files"
    log.info("GET %s", url)

    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, follow_redirects=True,
    ) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Список файлов %s: %s", url, e)
            return []

    return _parse_file_list(r.text)


def _parse_file_list(html: str) -> list[dict]:
    tree = LexborHTMLParser(html)
    files: list[dict] = []
    for row in tree.css("tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue
        name = _text(cells[0])
        size_text = _text(cells[1])
        if not name or name.lower() == "название":
            continue
        files.append({
            "name": name,
            "size_bytes": _parse_size(size_text),
            "size_text": size_text,
        })
    return files


# ============================================================
# ХЕЛПЕРЫ ПАРСИНГА (приватные)
# ============================================================

def _parse_row(row, base: str) -> dict | None:
    cells = row.css("td")
    if len(cells) < 4:
        return None

    added_at = _parse_date(_text(cells[0]))

    magnet_a = row.css_first('a[href^="magnet:"]')
    if magnet_a is None:
        return None
    magnet = magnet_a.attributes.get("href", "")
    m = _MAGNET_RE.search(magnet)
    if not m:
        return None
    torrent_hash = m.group(1).lower()

    title_a = None
    for a in row.css("a[href]"):
        href = a.attributes.get("href", "")
        if href.startswith("/torrent/"):
            title_a = a
            break
    if title_a is None:
        return None

    title_raw = _text(title_a)
    # href раздачи может содержать кириллицу/«ёлочки». Кодируем путь,
    # чтобы source_url был ASCII-совместимым (иначе он падал в Referer
    # при запросе .torrent-файла — см. manager.fetch_torrent_bytes).
    href = title_a.attributes.get("href", "")
    source_url = urljoin(base, quote(href, safe="/:@!$&'()*+,;=~-._"))

    size_bytes = None
    for cell in cells:
        sz = _parse_size(_text(cell))
        if sz is not None:
            size_bytes = sz
            break

    seeds, leeches = _parse_peers(row)

    return {
        "torrent_hash": torrent_hash,
        "magnet": magnet,
        "title_raw": title_raw,
        "source_url": source_url,
        "size_bytes": size_bytes,
        "seeds": seeds,
        "leeches": leeches,
        "added_at": added_at,
    }


def _text(node) -> str:
    if node is None:
        return ""
    return " ".join(node.text(strip=True).split())


def _parse_size(text: str) -> int | None:
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return int(value * _SIZE_MULT[m.group(2).upper()])


def _parse_size_bytes(value: str | None) -> int | None:
    if not value:
        return None
    m = _SIZE_BYTES_RE.search(value)
    if m:
        return int(m.group(1))
    return _parse_size(value)


def _parse_date(text: str) -> str | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS.get(m.group(2).lower()[:3])
    year = 2000 + int(m.group(3))
    if not month:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_full_date(value: str | None) -> str | None:
    if not value:
        return None
    m = _FULL_DATE_RE.search(value)
    if not m:
        return None
    day, month, year = m.groups()
    return f"{year}-{month}-{day}"


def _parse_duration(value: str | None) -> int | None:
    if not value:
        return None
    m = _DURATION_RE.search(value)
    if not m:
        return None
    h, mm, ss = map(int, m.groups())
    return h * 3600 + mm * 60 + ss


def _parse_peers(row) -> tuple[int | None, int | None]:
    seeds = leeches = None
    for span in row.css("span.green, span.red"):
        num = _first_int(_text(span))
        if num is None:
            continue
        cls = span.attributes.get("class", "")
        if "green" in cls:
            seeds = num
        elif "red" in cls:
            leeches = num
    return seeds, leeches


def _first_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _extract_cover(details, base: str) -> str | None:
    for img in details.css("img"):
        src = img.attributes.get("src", "")
        if not src or "cdnbunny" in src:
            continue
        if src.lower().endswith((".gif", ".png")):
            continue
        if src.startswith("//"):
            return "https:" + src
        return urljoin(base, src)
    return None


def _extract_labeled_fields(html: str) -> dict[str, str | None]:
    result: dict[str, str | None] = {}

    for m in _LABELED_FIELD_RE.finditer(html):
        key = m.group("key").strip().rstrip(":")
        key = _alias_label(key)
        val = _strip_tags(m.group("val"))
        if val:
            result[key] = val

    desc_m = _DESC_RE.search(html)
    if desc_m:
        result["Описание"] = _strip_tags(desc_m.group(1))

    return result


_LABEL_ALIASES = {
    "год выпуска":       "Год",
    "год издания книги": "Год",
    "исполнитель":       "Чтец",
    "озвучивает":        "Чтец",
    "читает":            "Чтец",
    "битрейт аудио":     "Битрейт",
    "формат/кодек":      "Формат",
}


def _alias_label(key: str) -> str:
    return _LABEL_ALIASES.get(key.strip().lower(), key.strip())


def _strip_tags(html_fragment: str) -> str:
    if not html_fragment:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _parse_related(details, base: str) -> list[dict]:
    fieldset = details.css_first("fieldset")
    if fieldset is None:
        return []
    items = []
    for row in fieldset.css("tr.gai, tr.tum"):
        item = _parse_row(row, base)
        if item:
            items.append(item)
    return items