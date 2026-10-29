"""
Парсер rutor.info — реализует протокол TorrentSource.

Особенности:
  - hidewrap → extract_series_info()
  - связанные раздачи → related
  - файл-лист → fetch_file_list()
  - scan() — обход страниц поиска с CrawlState (бывший crawler.py)
"""
from __future__ import annotations


import asyncio
import logging
import re
from datetime import datetime
from collections.abc import AsyncIterator
from urllib.parse import quote, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser

from app.config import settings
from app.catalog.description_parser import extract_hidewrap
from app.db.models.crawl_state import CrawlState
from app.scraper.normalize import (
    clean_title, detect_content_type, extract_author,
    extract_narrator, extract_year,
)
from app.scraper.types import (
    ProgressFn, SearchItem, SeriesEntry, SeriesInfo, TorrentDetails, TorrentSource,
)

log = logging.getLogger(__name__)

# ============================================================
# КОНСТАНТЫ ПАРСИНГА
# ============================================================

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

_LABEL_ALIASES = {
    "год выпуска":       "Год",
    "год издания книги": "Год",
    "исполнитель":       "Чтец",
    "озвучивает":        "Чтец",
    "читает":            "Чтец",
    "битрейт аудио":     "Битрейт",
    "формат/кодек":      "Формат",
}


# ---------------------------------------------------------------
# Модульные хелперы (не используют self)
# ---------------------------------------------------------------

def _strip_tags(html_fragment: str) -> str:
    if not html_fragment:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _alias_label(key: str) -> str:
    return _LABEL_ALIASES.get(key.strip().lower(), key.strip())


# ============================================================
# КЛАСС-ИСТОЧНИК
# ============================================================

class RutorSource:
    """
    Парсер и поставщик данных с rutor.info.

    Реализует протокол TorrentSource.
    Использует description_parser.extract_hidewrap() для извлечения состава серии.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.RUTOR_BASE_URL).rstrip("/")

    # ──────────────────────────────────────────────
    # Протокол TorrentSource
    # ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "rutor.info"

    @property
    def base_url(self) -> str:
        return self._base_url

    async def fetch_search_page(
        self, page: int, *, query: str = "mp3",
    ) -> list[SearchItem] | None:
        """
        Одна страница поиска.
        Возвращает None при ошибке сети, [] при пустой странице.
        """
        timeout = settings.SCRAPE_HTTP_TIMEOUT
        url = self._build_search_url(query, page)

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

        raw_items = self._parse_search_page(r.text)
        return [self._normalize_search_item(it) for it in raw_items]

    async def fetch_torrent_details(
        self, url_or_id: str | int,
    ) -> TorrentDetails | None:
        """Детальная страница раздачи."""
        timeout = settings.SCRAPE_HTTP_TIMEOUT
        url = self._torrent_url(url_or_id)

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

        return self._parse_and_normalize_details(r.text, source_url=url)

    async def fetch_file_list(
        self, torrent_id: int,
    ) -> list[dict]:
        """Список файлов внутри раздачи."""
        timeout = settings.SCRAPE_HTTP_TIMEOUT
        url = f"{self._base_url}/descriptions/{torrent_id}.files"
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

        return self._parse_file_list(r.text)

    def extract_series_info(self, html: str) -> SeriesInfo | None:
        """
        Извлекает состав цикла из hidewrap HTML.

        Пример:
          Цикл «Девятиэтажники»:
            1. Девятиэтажка
            2. Девятиэтажка 2
            ...

        Returns:
            SeriesInfo — если hidewrap найден.
            None — если hidewrap отсутствует или не содержит названия цикла.
        """
        result = extract_hidewrap(html)
        if result is None or not result.series_name:
            return None
        return SeriesInfo(
            name=result.series_name,
            entries=[
                SeriesEntry(order=e.order, title=e.title)
                for e in result.entries
            ],
        )

    # ──────────────────────────────────────────────
    # Внутренние методы (private)
    # ──────────────────────────────────────────────

    def _parse_and_normalize_details(self, html: str,
                                      source_url: str | None = None,
                                     ) -> TorrentDetails | None:
        """Парсит HTML и возвращает нормализованный TorrentDetails."""
        tree = LexborHTMLParser(html)
        details = tree.css_first("#details")
        if details is None:
            return None

        h1 = tree.css_first("h1")
        title_raw = self._text(h1) if h1 else None

        magnet_a = details.css_first('a[href^="magnet:"]')
        if magnet_a is None:
            return None

        magnet = magnet_a.attributes.get("href", "")
        m = _MAGNET_RE.search(magnet)
        if not m:
            return None
        torrent_hash = m.group(1).lower()

        # torrent_id
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

        cover_url = self._extract_cover(details)
        fields = self._extract_labeled_fields(html)
        rubrics = [self._text(a) for a in details.css('a[href^="/tag/"]')]
        related = self._parse_related(details)
        series_info = self.extract_series_info(html)

        # нормализация полей
        title = clean_title(title_raw)
        author = extract_author(title_raw)
        narrator = (
            fields.get("Чтец")
            or fields.get("Исполнитель")
            or fields.get("Озвучивает")
            or extract_narrator(title_raw)
        )
        year = self._first_int(fields.get("Год")) or extract_year(title_raw)
        content_type, books_hint = detect_content_type(title_raw)

        return TorrentDetails(
            torrent_id=torrent_id,
            torrent_hash=torrent_hash,
            magnet=magnet,
            source_url=source_url or (
                f"{self._base_url}/torrent/{torrent_id}" if torrent_id else None
            ),
            title=title,
            title_raw=title_raw,
            author=author,
            narrator=narrator,
            year=year,
            description=fields.get("Описание"),
            publisher=fields.get("Издательство"),
            format=fields.get("Формат"),
            duration_sec=self._parse_duration(fields.get("Продолжительность")),
            size_bytes=self._parse_size_bytes(fields.get("Размер")),
            rubrics=list(rubrics),
            cover_url=cover_url,
            content_type=content_type,
            books_hint=books_hint,
            series_info=series_info,
            related=related,
        )

    # ---------------------------------------------------------------
    # Поисковая выдача
    # ---------------------------------------------------------------

    def _build_search_url(self, query: str, page: int) -> str:
        return (
            f"{self._base_url}/search/{page}/{CATEGORY_BOOKS}/"
            f"{SEARCH_METHOD_ALL_WORDS}{SEARCH_IN_TITLE}/"
            f"{SORT_BY_DATE + SORT_DESC}/{query}"
        )

    def _torrent_url(self, url_or_id: str | int) -> str:
        if isinstance(url_or_id, int) or str(url_or_id).isdigit():
            return f"{self._base_url}/torrent/{url_or_id}"
        return str(url_or_id)

    def _parse_search_page(self, html: str) -> list[dict]:
        tree = LexborHTMLParser(html)
        items: list[dict] = []
        for row in tree.css("tr.gai, tr.tum"):
            item = self._parse_row(row)
            if item:
                items.append(item)
        return items

    def _normalize_search_item(self, item: dict) -> SearchItem:
        title_raw = item.get("title_raw")
        title = clean_title(title_raw) or "Без названия"
        content_type, books_hint = detect_content_type(title_raw)

        return SearchItem(
            torrent_hash=item["torrent_hash"],
            magnet=item["magnet"],
            source_url=item.get("source_url"),
            size_bytes=item.get("size_bytes"),
            added_at=item.get("added_at"),
            title_raw=title_raw,
            title=title,
            author=extract_author(title_raw),
            narrator=extract_narrator(title_raw),
            year=extract_year(title_raw),
            content_type=content_type,
            books_hint=books_hint,
        )

    # ---------------------------------------------------------------
    # Файл-лист
    # ---------------------------------------------------------------

    def _parse_file_list(self, html: str) -> list[dict]:
        tree = LexborHTMLParser(html)
        files: list[dict] = []
        for row in tree.css("tr"):
            cells = row.css("td")
            if len(cells) < 2:
                continue
            name = self._text(cells[0])
            size_text = self._text(cells[1])
            if not name or name.lower() == "название":
                continue
            files.append({
                "name": name,
                "size_bytes": self._parse_size(size_text),
                "size_text": size_text,
            })
        return files

    # ---------------------------------------------------------------
    # Парсинг строки поиска
    # ---------------------------------------------------------------

    def _parse_row(self, row) -> dict | None:
        cells = row.css("td")
        if len(cells) < 4:
            return None

        added_at = self._parse_date(self._text(cells[0]))

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

        title_raw = self._text(title_a)
        href = title_a.attributes.get("href", "")
        source_url = urljoin(self._base_url, quote(href, safe="/:@!$&'()*+,;=-._"))

        size_bytes = None
        for cell in cells:
            sz = self._parse_size(self._text(cell))
            if sz is not None:
                size_bytes = sz
                break

        seeds, leeches = self._parse_peers(row)

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

    # ---------------------------------------------------------------
    # Связанные раздачи
    # ---------------------------------------------------------------

    def _parse_related(self, details) -> list[dict]:
        fieldset = details.css_first("fieldset")
        if fieldset is None:
            return []
        items = []
        for row in fieldset.css("tr.gai, tr.tum"):
            item = self._parse_row(row)
            if item:
                items.append(item)
        return items

    # ---------------------------------------------------------------
    # Хелперы
    # ---------------------------------------------------------------

    @staticmethod
    def _text(node) -> str:
        if node is None:
            return ""
        return " ".join(node.text(strip=True).split())

    @staticmethod
    def _parse_size(text: str) -> int | None:
        m = _SIZE_RE.search(text or "")
        if not m:
            return None
        value = float(m.group(1).replace(",", "."))
        return int(value * _SIZE_MULT[m.group(2).upper()])

    @staticmethod
    def _parse_size_bytes(value: str | None) -> int | None:
        if not value:
            return None
        m = _SIZE_BYTES_RE.search(value)
        if m:
            return int(m.group(1))
        return RutorSource._parse_size(value)

    @staticmethod
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

    @staticmethod
    def _parse_full_date(value: str | None) -> str | None:
        if not value:
            return None
        m = _FULL_DATE_RE.search(value)
        if not m:
            return None
        day, month, year = m.groups()
        return f"{year}-{month}-{day}"

    @staticmethod
    def _parse_duration(value: str | None) -> int | None:
        if not value:
            return None
        m = _DURATION_RE.search(value)
        if not m:
            return None
        h, mm, ss = map(int, m.groups())
        return h * 3600 + mm * 60 + ss

    @staticmethod
    def _parse_peers(row) -> tuple[int | None, int | None]:
        seeds = leeches = None
        for span in row.css("span.green, span.red"):
            num = RutorSource._first_int(RutorSource._text(span))
            if num is None:
                continue
            cls = span.attributes.get("class", "")
            if "green" in cls:
                seeds = num
            elif "red" in cls:
                leeches = num
        return seeds, leeches

    @staticmethod
    def _first_int(text: str | None) -> int | None:
        if not text:
            return None
        m = re.search(r"\d+", text)
        return int(m.group()) if m else None

    def _extract_cover(self, details) -> str | None:
        for img in details.css("img"):
            src = img.attributes.get("src", "")
            if not src or "cdnbunny" in src:
                continue
            if src.lower().endswith((".gif", ".png")):
                continue
            if src.startswith("//"):
                return "https:" + src
            return urljoin(self._base_url, src)
        return None

    def _extract_labeled_fields(self, html: str) -> dict[str, str | None]:
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

    # ---------------------------------------------------------------
    # Обход страниц поиска (scan)
    # ---------------------------------------------------------------

    async def scan(
        self, query: str, state: CrawlState,
        on_progress: ProgressFn | None = None,
    ) -> AsyncIterator[list[SearchItem]]:
        """
        Обход страниц поиска с учётом CrawlState.

        Первый обход (state.oldest_seen_at is None) — снизу вверх,
        начиная с последней страницы.

        Последующие — сверху вниз, стоп на первой полностью старой странице.
        """
        if state.oldest_seen_at is None:
            async for page in self._initial_scan(query, state, on_progress):
                yield page
        else:
            async for page in self._incremental_scan(query, state, on_progress):
                yield page

    async def _initial_scan(
        self, query, state, on_progress,
    ) -> AsyncIterator[list[SearchItem]]:
        """Первый обход: последняя страница → 0."""
        if state.resume_page == 0:
            last_page = await self._find_last_page(query)
            if last_page is None:
                log.warning("Не удалось найти последнюю страницу")
                return

            last_items = await self.fetch_search_page(last_page, query=query)
            if not last_items:
                return

            last_date = self._parse_search_date(last_items[-1].added_at)
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

            items = await self.fetch_search_page(page, query=query)
            if not items:
                break

            yield items

            page -= 1
            state.resume_page = page
            if on_progress:
                on_progress(page, state)

    async def _incremental_scan(
        self, query, state, on_progress,
    ) -> AsyncIterator[list[SearchItem]]:
        """Свежие страницы: 0 → ... → стоп на старой."""
        page = 0
        while True:
            if page > 0:
                await asyncio.sleep(settings.SCRAPE_DELAY_SEC)

            items = await self.fetch_search_page(page, query=query)
            if not items:
                break

            if not self._has_fresh(items, state.oldest_seen_at):
                log.info("Страница %d вся старее %s, стоп",
                         page, state.oldest_seen_at.date())
                break

            yield items

            if len(items) < 100:
                break

            page += 1
            if on_progress:
                on_progress(page, state)

    async def _find_last_page(self, query: str) -> int | None:
        """Бинарный поиск последней доступной страницы."""
        for page in range(19, -1, -1):
            if page < 19:
                await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
            items = await self.fetch_search_page(page, query=query)
            if items:
                return page
        return None

    @staticmethod
    def _has_fresh(items: list[SearchItem], oldest_seen: datetime) -> bool:
        for item in items:
            d = RutorSource._parse_search_date(item.added_at)
            if d is not None and d > oldest_seen:
                return True
        return False

    @staticmethod
    def _parse_search_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None