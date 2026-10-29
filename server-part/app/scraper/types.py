"""
Типы и протоколы для парсеров торрент-трекеров.

Каждый источник (rutor.info, rutracker.org, …) реализует протокол TorrentSource:
  - fetch_search_page(query, page) -> list[SearchItem]
  - fetch_torrent_details(url) -> TorrentDetails | None
  - fetch_file_list(torrent_id) -> list[FileItem]
  - extract_series_info(html: str) -> SeriesInfo | None  — извлечение цикла
  - scan(query, state, on_progress) -> AsyncIterator[list[SearchItem]]

SearchItem      — запись поисковой выдачи (для find_or_create).
TorrentDetails  — детальная страница (для apply_details).
FileItem        — файл внутри торрента (для ingest_torrent).
SeriesInfo      — состав цикла/серии из описания раздачи.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.db.models.crawl_state import CrawlState


# ---------------------------------------------------------------
# Запись поисковой выдачи
# ---------------------------------------------------------------

@dataclass
class SearchItem:
    """Нормализованная запись поисковой выдачи."""
    torrent_hash: str
    title: str
    magnet: str
    content_type: str                         # "audio" | "print" | "video"
    source_url: str | None = None
    size_bytes: int | None = None
    books_hint: int | None = None
    author: str | None = None
    narrator: str | None = None
    title_raw: str | None = None              # оригинальный заголовок раздачи
    added_at: str | None = None               # дата в формате "2025-03-15"
    year: int | None = None


# ---------------------------------------------------------------
# Запись книги в составе цикла (hidewrap)
# ---------------------------------------------------------------

@dataclass
class SeriesEntry:
    """Одна книга цикла из описания раздачи."""
    order: int
    title: str


@dataclass
class SeriesInfo:
    """Состав цикла (серии) из описания раздачи.

    Извлекается из hidewrap HTML, специфичного для rutor.info.
    Другие источники могут заполнять иначе.
    """
    name: str                                 # название цикла, напр. «Девятиэтажники»
    entries: list[SeriesEntry] = field(default_factory=list)


# ---------------------------------------------------------------
# Детальная страница (TorrentDetails)
# ---------------------------------------------------------------

@dataclass
class TorrentDetails:
    """
    Нормализованные поля детальной страницы раздачи.

    Это контракт между scraper-модулем (rutor.py, rutracker.py, …)
    и Release.apply_details().
    """
    torrent_id: int | None = None
    torrent_hash: str | None = None
    magnet: str | None = None
    source_url: str | None = None

    # основные поля
    title: str | None = None
    title_raw: str | None = None
    author: str | None = None
    narrator: str | None = None
    year: int | None = None
    description: str | None = None
    publisher: str | None = None
    format: str | None = None                 # MP3, AAC, PDF, …

    # метрики
    duration_sec: int | None = None
    size_bytes: int | None = None

    # рубрики
    rubrics: list[str] = field(default_factory=list)

    # обложка
    cover_url: str | None = None

    # тип и подсказка количества книг
    content_type: str | None = None           # "audio" | "print" | "video"
    books_hint: int | None = None             # "[5 книг]" -> 5

    # состав цикла (серии) — извлекается из описания раздачи
    series_info: SeriesInfo | None = None

    # связанные раздачи
    related: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------
# Тип коллбэка прогресса сканирования
# ---------------------------------------------------------------

ProgressFn = Callable[[int, CrawlState], None]
"""Вызывается из scan() после обработки страницы: (page, state) -> None."""


# ---------------------------------------------------------------
# Протокол источника
# ---------------------------------------------------------------

@runtime_checkable
class TorrentSource(Protocol):
    """Протокол, которому должен соответствовать каждый источник."""

    @property
    def name(self) -> str:
        """Короткое имя источника (rutor.info, rutracker, …)."""
        ...

    @property
    def base_url(self) -> str:
        """Базовый URL для API источника."""
        ...

    async def fetch_search_page(
        self, page: int, *, query: str = "",
    ) -> list[SearchItem] | None:
        """Страница поисковой выдачи. None при ошибке сети."""
        ...

    async def fetch_torrent_details(
        self, url_or_id: str | int,
    ) -> TorrentDetails | None:
        """Детальная страница раздачи."""
        ...

    async def fetch_file_list(self, torrent_id: int) -> list[dict]:
        """Список файлов внутри раздачи."""
        ...

    def extract_series_info(self, html: str) -> SeriesInfo | None:
        """
        Извлекает состав серии из HTML-страницы раздачи.

        Для rutor.info — парсинг hidewrap (description_parser.extract_hidewrap).
        Для других источников может быть иная логика.
        Если источник не поддерживает извлечение серии — возвращает None.
        """
        ...

    def scan(
        self, query: str, state: CrawlState,
        on_progress: ProgressFn | None = None,
    ) -> AsyncIterator[list[SearchItem]]:
        """
        Обход страниц поиска с учётом CrawlState.

        Первый обход (state.oldest_seen_at is None) — снизу вверх,
        начиная с последней страницы.
        Последующие — сверху вниз, стоп на первой полностью старой странице.

        Args:
            query: Поисковый запрос (например "mp3").
            state: Состояние обхода (CrawlState ORM). Метод МОДИФИЦИРУЕТ его.
            on_progress: Коллбэк после каждой страницы (для сохранения прогресса).

        Yields:
            Страницы результатов поиска (list[SearchItem]).
        """
        ...