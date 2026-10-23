"""
Работа с обложками.

CoverService — единственное место, которое:
- скачивает обложку по внешнему URL (сразу при ingest, пока трекер жив),
- копирует обложку из папки торрента в кэш,
- возвращает клиенту стабильный URL /books/{id}/cover или /releases/{id}/cover.

Владелец обложки — любой объект с полями cover_* (Release, Book, Series).
Клиент не знает ни про источник обложки, ни про кэш — он всегда
запрашивает один и тот же URL, а сервер решает, что отдать.
"""

import logging
from pathlib import Path

import httpx

from app.catalog import layout
from app.config import settings

log = logging.getLogger(__name__)

# image/* → расширение файла
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/png":  "png",
    "image/webp": "webp",
    "image/gif":  "gif",
}

# расширения, которые считаем картинками, если MIME неизвестен
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class CoverService:
    """
    Скачивает, кэширует и резолвит обложки (Release/Book/Series).
    Живёт как singleton в app.state.covers.
    """

    def __init__(self, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "audiobook-server/1.0"},
        )
        log.info("CoverService инициализирован")

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------
    # Источники
    # ----------------------------------------------------------

    async def fetch_from_url(self, owner, url: str) -> bool:
        """
        Скачивает обложку по URL и кладёт в кэш.
        owner — Release, Book или Series: у него есть поля cover_*.

        Идемпотентно: если файл с таким sha256 уже есть, не копирует.

        Возвращает True, если обложка в итоге есть в кэше.
        """
        if not url:
            return False

        try:
            r = await self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Обложка не скачана %s: %s", url, e)
            return False

        mime = (r.headers.get("content-type") or "").split(";")[0].strip()
        ok = self._store(
            owner,
            data=r.content,
            mime=mime,
            origin="tracker",
            ext_hint=_ext_from_url(url),
        )
        return ok

    async def fetch_url_to_cache(self, url: str) -> dict | None:
        """
        Скачивает обложку по URL в кэш и возвращает метаданные
        ({cache_path, sha256, mime}) без привязки к ORM-объекту.

        Нужен, когда обложку скачивают вне открытой транзакции — например
        sync_catalog, чтобы не держать write-лок SQLite во время сети
        (иначе poller падает с "database is locked").
        """
        if not url:
            return None

        try:
            r = await self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Обложка не скачана %s: %s", url, e)
            return None

        mime = (r.headers.get("content-type") or "").split(";")[0].strip()
        return self._store_meta(
            data=r.content,
            mime=mime,
            origin="tracker",
            ext_hint=_ext_from_url(url),
        )

    def cache_from_torrent(self, owner, image_path: Path) -> bool:
        """
        Копирует файл обложки из папки торрента в кэш.
        Вызывается синхронно при сканировании, чтобы обложка не исчезла
        после TTL-удаления торрента.
        """
        if not image_path.exists():
            log.warning("Файл обложки отсутствует: %s", image_path)
            return False

        try:
            data = image_path.read_bytes()
        except OSError as e:
            log.warning("Не читается %s: %s", image_path, e)
            return False

        return self._store(
            owner,
            data=data,
            mime="",
            origin="torrent",
            ext_hint=image_path.suffix,
        )

    # ----------------------------------------------------------
    # Резолвинг для клиента
    # ----------------------------------------------------------

    def resolve_url(self, owner, api_base: str) -> str:
        """
        Клиент всегда получает один стабильный URL.
        Сервер сам решает: отдать из кэша или placeholder.

        Release → /releases/{id}/cover, Book → /books/{id}/cover.
        """
        from app.db.models import Book, Release

        if isinstance(owner, Release):
            return f"{api_base}/releases/{owner.id}/cover"
        if isinstance(owner, Book):
            return f"{api_base}/books/{owner.id}/cover"

        kind = type(owner).__name__
        return f"{api_base}/{kind.lower()}s/{owner.id}/cover"

    # ----------------------------------------------------------
    # Внутреннее
    # ----------------------------------------------------------

    def _store(self, owner, *, data: bytes, mime: str,
               origin: str, ext_hint: str = "") -> bool:
        """
        Общая логика сохранения: проверка размера, дедупликация по sha256,
        запись в кэш, обновление полей owner.cover_*.
        """
        meta = self._store_meta(
            data=data, mime=mime, origin=origin, ext_hint=ext_hint)
        if meta is None:
            return False

        owner.cover_origin = meta["origin"]
        owner.cover_cache_path = meta["cache_path"]
        owner.cover_sha256 = meta["sha256"]
        owner.cover_mime = meta["mime"]
        return True

    def _store_meta(self, *, data: bytes, mime: str,
                    origin: str, ext_hint: str = "") -> dict | None:
        """
        Сохраняет байты обложки в кэш и возвращает метаданные
        ({origin, cache_path, sha256, mime}) для записи в БД отдельной
        короткой транзакцией. Возвращает None при отказе.
        """
        if not data:
            log.warning("Пустая обложка для %s", origin)
            return None

        if len(data) > settings.COVER_MAX_BYTES:
            log.warning("Обложка отклонена: %d байт > %d",
                        len(data), settings.COVER_MAX_BYTES)
            return None

        ext = _resolve_ext(mime, ext_hint) or "jpg"
        sha = layout.sha256_bytes(data)
        target = layout.cover_file(sha, ext)

        if not target.exists():
            layout.atomic_write(target, data)
            log.info("Обложка сохранена: %s (%d байт, %s)",
                     target, len(data), origin)
        else:
            log.debug("Обложка уже в кэше: %s", target)

        return {
            "origin": origin,
            "cache_path": layout.cover_rel(sha, ext),
            "sha256": sha,
            "mime": mime or None,
        }


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def _resolve_ext(mime: str, hint: str) -> str | None:
    """
    Определяет расширение файла.
    Порядок: MIME → подсказка из имени файла.
    """
    if mime:
        base = mime.split(";")[0].strip().lower()
        if base in _MIME_TO_EXT:
            return _MIME_TO_EXT[base]

    if hint:
        h = hint.lower()
        if not h.startswith("."):
            h = "." + h
        if h in _IMAGE_EXTS:
            return h.lstrip(".")

    return None


def _ext_from_url(url: str) -> str:
    """Пытается вытащить расширение из конца URL."""
    path = url.split("?")[0].split("#")[0]
    _, _, ext = path.rpartition(".")
    return f".{ext.lower()}" if ext and len(ext) <= 5 else ""