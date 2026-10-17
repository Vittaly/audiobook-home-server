from pydantic import BaseModel


class PersonRef(BaseModel):
    """Краткая ссылка на персону внутри карточки книги."""
    name: str
    role: str