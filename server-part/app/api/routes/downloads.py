"""
Эндпоинты управления загрузкой книги.

Тонкий слой: HTTP-запрос → вызов доменной операции в catalog.download
→ маппинг error_code на HTTP-код → JSON.

Никаких деталей торрент-клиента, файловой системы или БД —
это всё в catalog.download.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import PriorityRequest
from app.catalog import download as download_domain
from app.catalog import service
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


def _raise_for(result: download_domain.DownloadResult) -> None:
    if result.success:
        return
    code = _ERROR_TO_HTTP.get(result.error_code or "", 500)
    raise HTTPException(code, result.error_message or "download failed")


# ============================================================
# POST /books/{id}/download
# ============================================================

@router.post("/books/{book_id}/download", status_code=202)
async def start_download(
    book_id: int,
    db: Session = Depends(get_db),
):
    """Запускает загрузку книги."""
    result = await download_domain.start(db, book_id)
    _raise_for(result)

    return {
        "book_id": result.book_id,
        "client_id": result.client_id,
        "status": result.status,
        "save_path": result.save_path,
        "message": "download started",
    }


# ============================================================
# DELETE /books/{id}/download
# ============================================================

@router.delete("/books/{id}/download")
def stop_download(
    book_id: int,
    delete_files: bool = False,
    db: Session = Depends(get_db),
):
    """Останавливает загрузку."""
    result = download_domain.stop(db, book_id, delete_files=delete_files)
    _raise_for(result)

    return {
        "book_id": result.book_id,
        "status": result.status,
        "message": "download stopped",
    }


# ============================================================
# GET /books/{id}/status
# ============================================================

@router.get("/books/{book_id}/status")
def download_status(
    book_id: int,
    db: Session = Depends(get_db),
):
    """Прогресс загрузки. Данные из БД и клиента — собирает сервис."""
    data = service.get_download_status(db, book_id)
    if not data:
        raise HTTPException(404, "book not found")
    return data


# ============================================================
# DELETE /books/{id}
# ============================================================

@router.delete("/books/{id}")
def delete_book(
    book_id: int,
    db: Session = Depends(get_db),
):
    """Удаляет торрент с файлами, метаданные книги сохраняются."""
    result = download_domain.remove(db, book_id)
    _raise_for(result)

    return {
        "book_id": result.book_id,
        "status": result.status,
        "message": "book deleted",
    }


# ============================================================
# POST /books/{id}/priority
# ============================================================

@router.post("/books/{book_id}/priority")
def set_priority(
    book_id: int,
    payload: PriorityRequest,
    db: Session = Depends(get_db),
):
    """
    Закрепление книги. Пока пишется в БД, дальнейшая логика TTL
    появится позже.
    """
    book = service.get_book(db, book_id)
    if book is None:
        raise HTTPException(404, "book not found")

    if book.status not in ("ready", "partial", "downloading"):
        raise HTTPException(
            409, f"нельзя закрепить книгу в статусе {book.status}")

    # TODO: book.priority = payload.priority; db.commit()

    return {
        "book_id": book.id,
        "priority": payload.priority,
        "message": "priority saved",
    }