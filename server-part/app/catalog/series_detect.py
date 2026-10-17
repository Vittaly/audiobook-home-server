"""
Автоматическое определение серии для книг.

Серия — атрибут любой книги (Book.series_id / Book.series_order).
Тома одной серии могут лежать в разных раздачах, поэтому поиск/создание
Series идёт глобально по каталогу (по нормализованному названию).

Источники уверенной информации:
  1. Название папки тома: «Хроники Амбера. Том 2» → серия + номер тома
     (группы structure.split_torrent_into_books уже содержат
      series_hint / volume).
  2. Заголовок раздачи (для однокнижной раздачи): «Автор - Дозоры. Том 1»
     → серия «Дозоры», номер тома 1.

Никакого ручного API. Если уверенно определить не удалось — поля остаются
NULL (книга просто не входит в серию).
"""

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Book, Release, Series

log = logging.getLogger(__name__)

# «Том 2», «Книга 1», «Часть 4» в заголовке
_TOME_IN_TITLE_RE = re.compile(
    r"(?:^|[\s.,:;_\-–—])(?:том|книга|часть)\s*[№#]?\s*(\d+)",
    re.IGNORECASE,
)

# слова, которые чаще встречаются в фанатских обзорах, чем в названиях серий
_NOT_SERIES_WORDS = re.compile(
    r"\b(?:аудиокнига|mp3|flac|m4b)\b", re.IGNORECASE,
)


def detect_series_for_book(db: Session, book: Book, release: Release,
                           group: dict) -> None:
    """
    Определяет серию книги по папке тома (group['series_hint']) или по
    заголовку раздачи и выставляет book.series_id / book.series_order.

    Вызывается при создании книги внутри раздачи (ingest_release).
    Уверенное совпадение → связываем (создаём Series при необходимости),
    иначе оставляем поля серии пустыми.
    """
    series_name: str | None = group.get("series_hint")
    volume: int | None = group.get("volume")

    # серия из папки не извлеклась → пробуем заголовок раздачи
    if not series_name:
        # только для однокнижных раздач (папки нет — папка тома = карточка)
        single = len(release.books) == 1
        if single or group.get("title") is None:
            name_from_title, vol_from_title = _series_from_release_title(
                release)
            if name_from_title:
                series_name = name_from_title
                volume = volume or vol_from_title

    if not series_name:
        return

    series_name = _clean_series_name(series_name)
    if not series_name or _NOT_SERIES_WORDS.search(series_name):
        return

    series = _ensure_series(db, series_name)
    book.series_id = series.id
    if volume is not None:
        book.series_order = volume

    log.info("book=%s: серия определена — %r (том %s), флаг уверенности",
             book.id, series.title, volume)


# ============================================================
# ВНУТРЕННЕЕ
# ============================================================

def _series_from_release_title(release: Release) -> tuple[str | None, int | None]:
    """
    «Сергей Лукьяненко - Дозоры. Том 1» → ('Дозоры', 1).
    Возвращает (series_name, volume) или (None, None).
    """
    title = release.title or ""
    m = _TOME_IN_TITLE_RE.search(title)
    if not m:
        return None, None

    volume = int(m.group(1))
    head = title[:m.start()].strip(" .-–—,;:")

    # убираем автора «Автор - Название»
    author = _release_author(release)
    if author and head.lower().startswith(author.lower()):
        head = head[len(author):].strip(" -–—.:,")

    head = head.strip(" .-–—,;:")
    if len(head) < 2:
        return None, None
    return head, volume


def _release_author(release: Release) -> str:
    for rp in release.persons:
        if rp.role == "author":
            return rp.person.name
    return ""


def _clean_series_name(name: str) -> str:
    name = name.strip(" .-–—,;:\"'«»()[]")
    return re.sub(r"\s+", " ", name).strip()


def _ensure_series(db: Session, name: str) -> Series:
    name_norm = _norm(name)
    series = db.execute(
        select(Series).where(Series.title_norm == name_norm)
    ).scalar_one_or_none()
    if series is None:
        series = Series(title=name, title_norm=name_norm)
        db.add(series)
        db.flush()
        log.info("series: создана новая %r", name)
    return series


def _norm(s: str) -> str:
    return " ".join(s.lower().split())
