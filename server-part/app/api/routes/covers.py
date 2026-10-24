"""
Отдача обложек.

Клиент всегда получает картинку: либо реальную обложку из кэша,
либо placeholder. Никогда не 404 — это упрощает UI, <img> не ломается.

Два типа владельцев обложки:
  /releases/{id}/cover — карточка раздачи (Release),
  /books/{id}/cover    — книга/том внутри раздачи (Book).

Заголовки:
  ETag          — sha256 обложки, для If-None-Match
  Cache-Control — долгий кэш на клиенте (7 дней)
  Last-Modified — если известна дата
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.catalog import layout
from app.db.models import Book, Release
from app.db.session import get_db

log = logging.getLogger(__name__)
router = APIRouter()

# путь к placeholder — один на всё приложение
_PLACEHOLDER = Path(__file__).resolve().parent.parent.parent.parent \
               / "app" / "static" / "placeholder.jpg"

# Заголовки кэширования для реальной обложки
_CACHE_HEADERS = {"Cache-Control": "public, max-age=604800"}

# Заголовки для временной обложки (книга показывает обложку раздачи,
# пока своя не получена): клиент должен перезапрашивать картинку,
# чтобы вовремя получить свою обложку после докачки/поиска.
_TEMP_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Cover-Temp": "true",
}


# GET и HEAD: клиенту плеера достаточно опросить заголовок X-Cover-Temp
# (HEAD — без тела картинки), чтобы узнать, финальная ли уже обложка.
@router.api_route("/books/{book_id}/cover", methods=["GET", "HEAD"])
def get_book_cover(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Отдаёт обложку книги (тома): из кэша или placeholder.

    Если у книги включена временная обложка (book.cover_is_temp) и своей
    обложки ещё нет — отдаём обложку карточки-раздачи сразу, как только
    сервер её знает, с заголовком X-Cover-Temp: true и Cache-Control:
    no-cache. Клиент перезапрашивает картинку и вовремя получит свою обложку
    после докачки файла или онлайн-поиска.

    Обложка раздачи отдаётся без обязательного условия, что она уже в кэше:
    если она есть — вернём её; если ещё нет — вернём placeholder, но всё
    равно с X-Cover-Temp: true, чтобы клиент не принял заглушку за финальную
    обложку и продолжил опрос.

    Метод HEAD возвращает те же заголовки без тела — для дешёвого опроса
    клиентом (плеер проверяет X-Cover-Temp, пока не получит финальную).
    """
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(404, "book not found")

    # Временная обложка: своя ещё не получена — отдаём обложку раздачи
    # (или placeholder с тем же признаком) с X-Cover-Temp: true.
    if book.cover_is_temp and not book.cover_cache_path:
        release = book.release
        if release is not None:
            return _serve_temp_release_cover(release, request)

    return _serve_cover("book", book_id, book, request)


def _serve_temp_release_cover(release: Release, request: Request) -> Response:
    """
    Отдаёт обложку раздачи как временную обложку книги.
    Добавляет X-Cover-Temp: true и Cache-Control: no-cache, чтобы
    клиент перезапрашивал, пока своя обложка книги не получена.
    """
    abs_path = layout.abs_cover(release.cover_cache_path)
    if release.cover_cache_path and abs_path.exists():
        etag = f'"{release.cover_sha256}"' if release.cover_sha256 else None

        if etag and request.headers.get("if-none-match") == etag:
            log.debug("temp cover 304 (not modified)")
            return Response(
                status_code=304,
                headers={**{"ETag": etag}, **_TEMP_HEADERS},
            )

        headers = {**_TEMP_HEADERS, "Accept-Ranges": "bytes"}
        if etag:
            headers["ETag"] = etag

        log.debug("temp cover из кэша раздачи %s", release.cover_cache_path)
        return FileResponse(
            abs_path,
            media_type=release.cover_mime or "image/jpeg",
            headers=headers,
        )

    # обложки раздачи нет — заглушка, но с признаком временного состояния
    return _placeholder_response(headers=_TEMP_HEADERS, method=request.method)


@router.get("/releases/{release_id}/cover")
def get_release_cover(
    release_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Отдаёт обложку карточки раздачи: из кэша или placeholder.
    Поддерживает ETag — при If-None-Match вернёт 304.
    """
    release = db.get(Release, release_id)
    if release is None:
        raise HTTPException(404, "release not found")
    return _serve_cover("release", release_id, release, request)


def _serve_cover(kind: str, owner_id: int, owner, request: Request) -> Response:
    """Общий отдатчик обложки: кэш → placeholder."""
    # --- реальная обложка из кэша ---
    if owner.cover_cache_path:
        abs_path = layout.abs_cover(owner.cover_cache_path)
        if abs_path.exists():
            etag = f'"{owner.cover_sha256}"' if owner.cover_sha256 else None

            # проверяем If-None-Match — клиент уже имеет эту версию
            if etag and request.headers.get("if-none-match") == etag:
                log.debug("%s=%s: cover 304 (not modified)", kind, owner_id)
                return Response(
                    status_code=304,
                    headers={**{"ETag": etag}, **_CACHE_HEADERS},
                )

            headers = {**_CACHE_HEADERS, "Accept-Ranges": "bytes"}
            if etag:
                headers["ETag"] = etag

            log.debug("%s=%s: cover из кэша %s",
                      kind, owner_id, owner.cover_cache_path)
            return FileResponse(
                abs_path,
                media_type=owner.cover_mime or "image/jpeg",
                headers=headers,
            )
        else:
            log.warning("%s=%s: cover_cache_path=%r, файл не найден",
                        kind, owner_id, owner.cover_cache_path)

    # --- placeholder ---
    log.debug("%s=%s: cover placeholder", kind, owner_id)
    return _placeholder_response(method=request.method)


# ============================================================
# PLACEHOLDER
# ============================================================

def _placeholder_response(headers: dict | None = None,
                          method: str = "GET") -> Response:
    """
    Возвращает картинку-заглушку или минимальный SVG, если её нет.

    Для метода HEAD тело не отдаём (только заголовки) — нужно, чтобы
    дешёвый опрос обложки клиентом был корректным. FileResponse сам
    учитывает HEAD, а inline-Response — нет, поэтому обрабатываем явно.
    """
    base = {"Cache-Control": "public, max-age=86400"}
    if headers:
        base.update(headers)

    is_head = (method or "GET").upper() == "HEAD"

    if _PLACEHOLDER.exists():
        return FileResponse(
            _PLACEHOLDER,
            media_type="image/jpeg",
            headers=base,
        )

    # если файла нет — отдаём inline SVG (плейсхолдер-заглушка)
    # это гарантирует, что клиент всегда получит 200 с картинкой
    svg = _SVG_PLACEHOLDER.encode("utf-8")            # явное кодирование
    return Response(
        content=None if is_head else svg,
        media_type="image/svg+xml",
        headers=base,
    )


_SVG_PLACEHOLDER = """...Нет обложки..."""        # обычная строка (Unicode)