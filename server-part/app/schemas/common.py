from pydantic import BaseModel


class ListResponse[T](BaseModel):
    """Обёртка для пагинированных списков."""
    items: list[T]
    total: int
    limit: int
    offset: int