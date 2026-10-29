"""
Value Object: обложка книги или раздачи.

Не имеет собственной таблицы в БД — оперирует полями cover_*
своего владельца (Book или Release) через self._owner.cover_*.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.catalog.covers import CoverService
    from app.db.models.book import Book
    from app.db.models.release import Release

log = logging.getLogger(__name__)


class Cover:
    """Value Object: обложка книги или раздачи.

    Не имеет своей таблицы — оперирует полями cover_* владельца
    (Book или Release) через self._owner.cover_*.

    Owner должен быть привязан к сессии SQLAlchemy, чтобы изменения
    полей cover_* попали в БД при commit.
    """

    RETRY_LIMIT = 3

    def __init__(self, owner: "Book | Release") -> None:
        self._owner = owner

    # ----------------------------------------------------------
    # Свойства
    # ----------------------------------------------------------

    @property
    def is_settled(self) -> bool:
        """Постоянная обложка есть (не временная и не отсутствующая)."""
        return self._owner.cover_cache_path is not None

    @property
    def needs_search(self) -> bool:
        """Нужен ли онлайн-поиск обложки.

        Условия:
        - нет кэшированного файла (cover_cache_path is None),
        - обложка помечена как временная (cover_is_temp),
        - не превышен лимит ретраев.
        """
        return (
            self._owner.cover_cache_path is None
            and self._owner.cover_is_temp
            and (self._owner.cover_retry_count or 0) < self.RETRY_LIMIT
        )

    # ----------------------------------------------------------
    # Мутаторы (работают с полями self._owner.cover_*)
    # ----------------------------------------------------------

    def apply_meta(self, meta: dict, origin: str, source_url: str = "") -> None:
        """Записывает метаданные скачанной обложки во владельца.

        Args:
            meta: Словарь от CoverService._store_meta() —
                  {cache_path, sha256, mime, ...}.
            origin: Происхождение обложки (tracker, search, torrent).
            source_url: URL, откуда скачана обложка.
        """
        self._owner.cover_origin = origin
        self._owner.cover_source_url = source_url or meta.get("source_url", "")
        self._owner.cover_cache_path = meta["cache_path"]
        self._owner.cover_sha256 = meta["sha256"]
        self._owner.cover_mime = meta.get("mime")

    def bump_retry(self) -> None:
        """Увеличивает счётчик ретраев (cover_retry_count += 1)."""
        self._owner.cover_retry_count = (self._owner.cover_retry_count or 0) + 1

    def set_temp(self) -> None:
        """Помечает обложку как временную (cover_is_temp = True).

        Вызывается при старте загрузки книги, чтобы при следующем
        просмотре карточки запустился поиск настоящей обложки.
        """
        self._owner.cover_is_temp = True

    # ----------------------------------------------------------
    # Загрузка
    # ----------------------------------------------------------

    def copy_from_torrent(self, hint_path: Path, covers: "CoverService") -> bool:
        """Копирует файл обложки из папки торрента в кэш.

        Args:
            hint_path: Путь к файлу обложки внутри торрента.
            covers: CoverService для копирования в кэш.

        Returns:
            True, если обложка успешно сохранена в кэш.
        """
        return covers.cache_from_torrent(self._owner, hint_path)

    async def start_download(
        self,
        title: str = "",
        author: str = "",
        rubrics: list[str] | None = None,
        url: str | None = None,
        covers: "CoverService | None" = None,
    ) -> bool:
        """Загружает обложку.

        Стратегия (в порядке приоритета):

        1. **Прямой URL** — если передан ``url``, скачиваем по нему
           (например, URL с трекера).
        2. **Поиск** — цепочка cover_search (LitRes → LiveLib →
           Google Books → Open Library → iTunes → Audnexus).
        3. **При успехе** — apply_meta(), caller отвечает за commit.

        Args:
            title: Название книги (для поиска).
            author: Автор (для поиска).
            rubrics: Рубрики (для поиска, опционально).
            url: Прямой URL обложки (например, с трекера).
            covers: CoverService для скачивания и кэширования.

        Returns:
            True, если обложка загружена и метаданные записаны.

        Note:
            Метод не управляет сессией БД — owner должен быть
            прикреплён к активной сессии, а caller отвечает за commit.
        """
        # --- 1. Прямой URL ---
        if url:
            if covers is None:
                log.warning("Cover.start_download: url=%s, но covers=None", url[:80])
                return False
            meta = await covers.fetch_url_to_cache(url)
            if meta is None:
                self.bump_retry()
                return False
            self.apply_meta(meta, origin="tracker", source_url=url)
            return True

        # --- 2. Поиск ---
        if not title:
            log.warning("Cover.start_download: не передан title для поиска")
            return False

        from app.catalog.cover_search import search_cover

        result = await search_cover(title, author, rubrics)
        if result is None:
            self.bump_retry()
            return False

        if covers is None:
            log.warning(
                "Cover.start_download: найден %s, но covers=None", result.url[:80]
            )
            return False

        meta = await covers.fetch_url_to_cache(result.url)
        if meta is None:
            self.bump_retry()
            return False

        self.apply_meta(meta, origin="search", source_url=result.url)
        return True