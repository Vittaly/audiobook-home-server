"""
Нормализация данных, полученных с rutor.info.

Все функции чистые, без БД, без сети.
detect_content_type возвращает строки ('single'|'series'|'collection'),
чтобы scraper не зависел от enum-ов из app.db.models.
"""

import re


# ============================================================
# ОЧИСТКА НАЗВАНИЙ
# ============================================================

# Мусор в заголовках раздач rutor
_NOISE_PATTERNS = [
    r"\[Аудиокнига\]",
    r"\[аудиокнига\]",
    r"\[MP3[^\]]*\]",
    r"\[MP3\]",
    r"\bMP3\b",
    r"\bMP4\b",
    r"\bFB2\b",
    r"\b128\s*kbps\b",
    r"\b192\s*kbps\b",
    r"\b256\s*kbps\b",
    r"\b320\s*kbps\b",
    r"\bторрент\b",
    r"\bЛитРес:\s*чтец\b",
    r"\bЛитРес\b",
    r"\bLitres\b",
    # «[5 книг]», «[Книга 1-9]», «[01-11 из 11]»
    r"\[\s*книга\s*\d+\s*[-–—]\s*\d+\s*\]",
    r"\[\s*\d+\s*книг[а-я]*\s*\]",
    r"\[\s*\d+\s*[-–—]\s*\d+\s*из\s*\d+\s*\]",
    r"\[\s*\d+\s*из\s*\d+\s*\]",
    # «(5 книг из 7)», «(5 книг)»
    r"\(\s*\d+\s*книг[а-я]*\s*(?:из\s*\d+)?\s*\)",
    # «(Книга 1-9)», «(том 2)»
    r"\(\s*(?:книга|том)\s*\d+(?:\s*[-–—]\s*\d+)?\s*\)",
    # «[сборник]», «(серия)»
    r"\[\s*(?:сборник|серия|цикл)\s*\]",
    r"\(\s*(?:сборник|серия|цикл)\s*\)",
]

# «(2026) MP3» в самом конце
_TRAILING_FORMAT_RE = re.compile(
    r"\s*\(.*?\)\s*(?:MP3|MP4|FB2|M4B|FLAC)?\s*$"
)

_AUTHOR_SEP_RE = re.compile(r"\s+[—–-]\s+")

_YEAR_RE = re.compile(r"\((\d{4})\)")
_YEAR_RANGE_RE = re.compile(r"\((\d{4})\s*[-–—]\s*(\d{4})\)")

# «(Иван Иванов)» — короткая скобка, похожая на ФИО
_NARRATOR_PAREN_RE = re.compile(r"\(([А-ЯЁA-Z][^()]{2,80})\)")

# «ЛитРес: чтец» внутри скобок
_NARRATOR_LITRES_RE = re.compile(
    r"\(([^()]*?ЛитРес[^()]*?)\)", re.IGNORECASE,
)

# «[читает Иван Иванов]»
_NARRATOR_BRACKETS_RE = re.compile(
    r"\[\s*(?:читает|озвучивает|исполнитель)[:\s]+([^\]]+)\]",
    re.IGNORECASE,
)

_COLLECTION_WORDS = re.compile(
    r"\b(?:сборник|собрание\s+сочинений|избранное|антология)\b",
    re.IGNORECASE,
)

_SERIES_WORDS = re.compile(
    r"\b(?:цикл|серия|книга\s*\d+|том\s*\d+|часть\s*\d+)\b",
    re.IGNORECASE,
)

_BOOKS_HINT_RE = re.compile(
    r"(?:\[|\()\s*(?:книга\s*[1-9]\d*\s*[-–—]\s*)?(\d+)\s*книг",
    re.IGNORECASE,
)

_MULTI_SPACE_RE = re.compile(r"\s+")


# ============================================================
# ПУБЛИЧНЫЕ ФУНКЦИИ
# ============================================================

def clean_title(raw: str | None) -> str:
    """
    Убирает мусор и формат-хвосты из названия раздачи.

    'Александр Алексеев - Нападающий вратарь [5 книг] (2026) MP3'
        → 'Александр Алексеев - Нападающий вратарь'
    """
    if not raw:
        return ""

    title = raw.strip()
    for pat in _NOISE_PATTERNS:
        title = re.sub(pat, " ", title, flags=re.IGNORECASE)

    # убираем хвост «(2026) MP3» в самом конце
    title = _TRAILING_FORMAT_RE.sub("", title)

    title = _MULTI_SPACE_RE.sub(" ", title)
    return title.strip(" -–—.")


def extract_author(raw: str | None) -> str | None:
    """
    'Александр Алексеев - Нападающий вратарь ...' → 'Александр Алексеев'.
    Возвращает None, если префикса-автора нет или он не похож на имя.
    """
    if not raw:
        return None

    if _AUTHOR_SEP_RE.search(raw) is None:
        return None

    head = _AUTHOR_SEP_RE.split(raw, maxsplit=1)[0].strip()
    head = _MULTI_SPACE_RE.sub(" ", head)

    if not head or head.isdigit():
        return None
    if len(head) > 120:
        return None
    if head.lower() in {"сборник", "разное", "различные авторы"}:
        return None

    return head


def extract_year(raw: str | None) -> int | None:
    """
    '(2026)' → 2026.
    '(2021-2026)' → 2026 (максимальный).
    """
    if not raw:
        return None

    m = _YEAR_RANGE_RE.search(raw)
    if m:
        return int(m.group(2))

    years = [int(y) for y in _YEAR_RE.findall(raw)]
    return max(years) if years else None


def extract_narrator(raw: str | None) -> str | None:
    """
    Пытается вытащить чтеца. Поддерживаемые форматы:
      - '[читает Иван Иванов]'
      - '(Иван Иванов)' — короткая скобка с ФИО
      - '(Автор, ЛитРес: чтец)' — берём элемент перед 'ЛитРес'

    Возвращает None, если ничего похожего нет.
    """
    if not raw:
        return None

    # 1. явный маркер в квадратных скобках
    m = _NARRATOR_BRACKETS_RE.search(raw)
    if m:
        candidate = m.group(1).strip()
        if candidate:
            return candidate

    # 2. «ЛитРес: чтец» — имя перед ним
    m = _NARRATOR_LITRES_RE.search(raw)
    if m:
        inner = m.group(1)
        parts = [p.strip() for p in inner.split(",")]
        for i, p in enumerate(parts):
            if "чтец" in p.lower() and i > 0:
                return parts[i - 1]

    # 3. короткая скобка с ФИО
    for m in _NARRATOR_PAREN_RE.finditer(raw):
        candidate = m.group(1).strip()
        # пропускаем года и нумерацию
        if candidate.isdigit():
            continue
        # год в скобках
        if re.fullmatch(r"\d{4}(?:\s*[-–—]\s*\d{4})?", candidate):
            continue
        # «5 книг» и т.п.
        if "книг" in candidate.lower():
            continue
        # «Книга 1-9», «Том 2»
        if re.match(r"^(?:книга|том|часть)\s*\d", candidate, re.I):
            continue

        words = candidate.split()
        if 1 < len(words) <= 4 and all(w[:1].isupper() for w in words):
            return candidate

    return None


def detect_content_type(title_raw: str | None) -> tuple[str, int | None]:
    """
    По заголовку раздачи определяет тип содержимого и число книг.

    Возвращает ('single'|'series'|'collection', books_hint | None).
    Уточняется на этапе скана по количеству папок в торренте.

    Приоритет:
      1. Явный маркер 'сборник' / 'собрание' / 'избранное' → collection.
      2. Явный маркер 'цикл' / 'серия' / 'книга N' → series.
      3. '[5 книг]' без слов → series.
      4. Ничего → single (уточняется в scan.py).
    """
    if not title_raw:
        return ("single", None)

    hint = _parse_books_hint(title_raw)

    if _COLLECTION_WORDS.search(title_raw):
        return ("collection", hint)

    if _SERIES_WORDS.search(title_raw):
        return ("series", hint)

    if hint and hint > 1:
        return ("series", hint)

    return ("single", None)


# ============================================================
# ВНУТРЕННЕЕ
# ============================================================

def _parse_books_hint(text: str) -> int | None:
    m = _BOOKS_HINT_RE.search(text)
    return int(m.group(1)) if m else None