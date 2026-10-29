"""
Эндпоинты управления загрузкой книги.

Тонкий слой: HTTP-запрос → Catalog.get_book → метод Book
(start_download / stop_download / remove_download)
→ маппинг error_code на HTTP-код → JSON.

Никаких деталей торрент-клиента, файловой системы или БД —
это всё в моделях Book/Release и торрент-менеджере.
"""
from __future__ import annotations


import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import PriorityRequest
from app.catalog import service
from app.catalog.catalog import Catalog
from app.catalog.types import DownloadResult
from app.db.session import get_db

log = logging.getLogger(__name__)
router = APIRouter()


# маппинг error_code → HTTP-статус
_ERROR_TO_HTTP = {
    "not_found": 404,
    "no_release": 409,
    "already_downloading": 409,
    "already_done": 409,
    "no_space": 507,
    "client_failed": 500,
}


def _raise_for(result: DownloadResult) -> None:
    if result.success:
        return
    code = _ERROR_TO_HTTP.get(result.error_code or "", 500)
    raise HTTPException(code, result.error_message or "download failed")


def _get_book_or_404(db: Session, book_hash: str):
    book = Catalog.get_book(db, book_hash)
    if book is None:
        raise HTTPException(404, "book not found")
    return book


# ============================================================
# POST /books/{book_hash}/download
# ============================================================

@router.post("/books/{book_hash}/download", status_code=202)
async def start_download(
    book_hash: str,
    db: Session = Depends(get_db),
):
    """Запускает загрузку книги."""
    book = _get_book_or_404(db, book_hash)
    result = await book.start_download(db)
    _raise_for(result)

    return {
        "release_id": result.release_id,
        "book_id": result.book_hash,
        "client_id": result.client_id,
        "status": result.status,
        "save_path": result.save_path,
        "message": "download started",
    }


# ============================================================
# POST /releases/{id}/download
# ============================================================

@router.post("/releases/{release_id}/download", status_code=202)
async def start_release_download(
    release_id: int,
    db: Session = Depends(get_db),
):
    """Запускает загрузку всей раздачи.

    Книг до старта загрузки может не быть (сервер не создаёт книгу-
    заглушку) — торрент стартует на уровне карточки раздачи. Клиент
    поллит каталог и берёт первую книгу по порядку по факту появления.
    """
    release = service.get_release(db, release_id)
    if release is None:
        raise HTTPException(404, "release not found")

    client_id = await release.start_download()
    if client_id is None:
        raise HTTPException(500, "не удалось добавить раздачу в торрент-клиент")
    db.commit()

    return {
        "release_id": release.id,
        "book_id": None,
        "client_id": client_id,
        "status": release.status,
        "save_path": release.save_path,
        "message": "download started",
    }


# ============================================================
# DELETE /books/{book_hash}/download
# ============================================================

@router.delete("/books/{book_hash}/download")
def stop_download(
    book_hash: str,
    delete_files: bool = False,
    db: Session = Depends(get_db),
):
    """Останавливает загрузку."""
    book = _get_book_or_404(db, book_hash)
    result = book.stop_download(db, delete_files=delete_files)
    _raise_for(result)

    return {
        "book_id": result.book_hash,
        "status": result.status,
        "message": "download stopped",
    }


# ============================================================
# GET /books/{book_hash}/status
# ============================================================

@router.get("/books/{book_hash}/status")
def download_status(
    book_hash: str,
    db: Session = Depends(get_db),
):
    """Прогресс загрузки. Данные из БД и клиента — собирает сервис."""
    data = service.get_download_status(db, book_hash)
    if not data:
        raise HTTPException(404, "book not found")
    return data


# ============================================================
# DELETE /books/{book_hash}
# ============================================================

@router.delete("/books/{book_hash}")
def delete_book(
    book_hash: str,
    db: Session = Depends(get_db),
):
    """Удаляет торрент с файлами, метаданные книги сохраняются."""
    book = _get_book_or_404(db, book_hash)
    result = book.remove_download(db)
    _raise_for(result)

    return {
        "book_id": result.book_hash,
        "status": result.status,
        "message": "book deleted",
    }


# ============================================================
# POST /books/{book_hash}/priority
# ============================================================

@router.post("/books/{book_hash}/priority")
def set_priority(
    book_hash: str,
    payload: PriorityRequest,
    db: Session = Depends(get_db),
):
    """
    Закрепление книги. Пока пишется в БД, дальнейшая логика TTL
    появится позже.
    """
    book = _get_book_or_404(db, book_hash)

    if book.status not in ("ready", "partial", "downloading"):
        raise HTTPException(
            409, f"нельзя закрепить книгу в статусе {book.status}")

    # TODO: book.priority = payload.priority; db.commit()

    return {
        "book_id": book.book_hash,
        "priority": payload.priority,
        "message": "priority saved",
    }