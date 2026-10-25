"""
HTTP-специфичные схемы.

Не описывают сущности — они в app/schemas/.
Здесь только тела запросов и параметры команд.
"""
from __future__ import annotations


from pydantic import BaseModel, Field


class PriorityRequest(BaseModel):
    """Тело POST /books/{id}/priority."""
    priority: int = Field(
        ...,
        ge=0, le=1,
        description="0 — обычная книга, 1 — закреплённая (не удалять по TTL)",
    )


class DownloadRequest(BaseModel):
    """Тело POST /books/{id}/download (когда понадобится)."""
    mode: str = Field(
        default="stream",
        pattern="^(stream|offline)$",
        description="stream — стриминг по требованию, offline — полная загрузка",
    )


class DeleteResponse(BaseModel):
    """Ответ на DELETE-операции."""
    book_id: int
    state: str
    message: str | None = None