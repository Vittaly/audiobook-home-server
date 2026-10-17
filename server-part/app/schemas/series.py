from pydantic import BaseModel


class SeriesRef(BaseModel):
    """Краткая ссылка на серию внутри карточки книги."""
    id: int
    title: str
    order: int | None = None