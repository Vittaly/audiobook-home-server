"""
Парсинг описания раздачи rutor.info для определения состава цикла (серии).

Из описания (HTML-фрагмента без hidewrap) и содержимого hidearea
извлекаем список книг, входящих в цикл, вместе с номерами томов.

Примеры реальных страниц см. rutor_page_examples/.

hidearea содержит HTML-escaped разметку: <b>Название</b>.
После unescape'а получаем:
  <b>Книга I</b><br/>
  2. <b>Книга II</b><br/>
  3. Название без bold<br/>

Стратегия извлечения номера тома:
  1. Римские цифры после слова «Книга» (Книга I, Книга II … Книга X, Книга XI…)
  2. Арабские цифры в начале строки: „N. Название“ или „N) Название“
  3. Если hidehead содержит диапазон «[Книга 1-15]» — используем его
     (из заголовка раздачи, не из hidearea)
  4. Если ничего не удалось — нумеруем по порядку появления в списке
"""
from __future__ import annotations

import html
import logging
import re
from typing import NamedTuple

log = logging.getLogger(__name__)

# Регулярка для hidewrap: <div class="hidewrap"> ... <textarea class="hidearea">...</textarea>
_HIDEWRAP_RE = re.compile(
    r'<div class="hidewrap">'
    r'.*?'
    r'<textarea class="hidearea">(.*?)</textarea>',
    re.DOTALL | re.IGNORECASE,
)

# Римские цифры от I до XXXIX
_ROMAN_RE = re.compile(
    r"\b(?:книга|том)\s*("
    r"XXXIX|XXXVIII|XXXVII|XXXVI|XXXV|XXXIV|XXXIII|XXXII|XXXI|"
    r"XXX|XXIX|XXVIII|XXVII|XXVI|XXV|XXIV|XXIII|XXII|XXI|"
    r"XX|XIX|XVIII|XVII|XVI|XV|XIV|XIII|XII|XI|"
    r"X|IX|VIII|VII|VI|V|IV|III|II|I)\b",
    re.IGNORECASE,
)

# Арабские цифры в начале строки: "N. Название" или "N) Название"
_ARABIC_LEAD_RE = re.compile(
    r"^\s*(\d+)\s*[.)]\s*",
)

# Удаление мусора внутри строки после номера
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")


class HidewrapEntry(NamedTuple):
    """Одна книга из состава цикла (hidewrap)."""
    order: int
    title: str


class HidewrapResult(NamedTuple):
    """Результат парсинга описания."""
    series_name: str | None  # название цикла из hidehead, None если hidewrap не найден
    entries: list[HidewrapEntry]  # список книг


def extract_hidewrap(html_text: str) -> HidewrapResult | None:
    """
    Извлекает hidewrap из полного HTML детальной страницы раздачи.

    Возвращает HidewrapResult с названием цикла и списком книг,
    или None если hidewrap не найден.
    """
    m = _HIDEWRAP_RE.search(html_text)
    if not m:
        return None

    hidearea_raw = m.group(1)
    # unescape HTML-entities: < → <, > → >, & → &, " → "
    hidearea = html.unescape(hidearea_raw)

    # Извлекаем название серии из hidehead (между hidewrap и hidearea)
    # Ищем первый текст до <textarea class="hidearea">
    head_m = re.search(
        r'<div class="hidehead"[^>]*>(.*?)</div>',
        html_text[m.start():m.end()],
        re.DOTALL | re.IGNORECASE,
    )
    series_name = None
    if head_m:
        head_text = _STRIP_TAGS_RE.sub("", head_m.group(1)).strip()
        # Убираем двоеточие в конце
        head_text = head_text.rstrip(" :")
        # Убираем префикс "Цикл «" / "Содержание"
        if head_text.lower().startswith("цикл"):
            # "Цикл «Имя»:" → "Имя"
            m_cycle = re.search(r"«([^»]+)»", head_text)
            if m_cycle:
                series_name = m_cycle.group(1)
            else:
                series_name = head_text
        elif head_text.lower().startswith("содержание"):
            series_name = head_text
        else:
            series_name = head_text

    # Разбираем hidearea на строки
    # Строки разделены <br/> или <br>
    lines = re.split(r"<br\s*/?>", hidearea)

    entries: list[HidewrapEntry] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Пробуем римские
        roman_m = _ROMAN_RE.search(line)
        if roman_m:
            order = _roman_to_int(roman_m.group(1))
            title = _clean_title(line)
            if title:
                entries.append(HidewrapEntry(order=order, title=title))
            continue

        # Пробуем арабские в начале
        arabic_m = _ARABIC_LEAD_RE.match(line)
        if arabic_m:
            order = int(arabic_m.group(1))
            title = _clean_title(line[arabic_m.end():])
            if title:
                entries.append(HidewrapEntry(order=order, title=title))
            continue

        # Ничего не нашли — пробуем извлечь название хотя бы
        title = _clean_title(line)
        if title:
            # Номер не определён — будет назначен по порядку
            entries.append(HidewrapEntry(order=0, title=title))

    # Назначаем порядковые номера тем, у кого order=0
    next_order = 1
    for i, entry in enumerate(entries):
        if entry.order == 0:
            # Если предыдущий entry имеет известный номер, продолжаем с него + 1
            if i > 0 and entries[i - 1].order != 0:
                next_order = entries[i - 1].order + 1
            entries[i] = HidewrapEntry(order=next_order, title=entry.title)
            next_order += 1

    if not entries:
        return None

    return HidewrapResult(series_name=series_name, entries=entries)


def _clean_title(raw: str) -> str | None:
    """Очищает строку от HTML-тегов и лишних пробелов."""
    text = _STRIP_TAGS_RE.sub("", raw)
    text = text.strip(" \t\n\r.-–—,;:\"'«»()[]")
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) < 2:
        return None
    return text


def _roman_to_int(s: str) -> int:
    """Преобразует римское число в арабское (I=1 … XXXIX=39)."""
    roman_map = {
        'I': 1, 'V': 5, 'X': 10, 'L': 50,
    }
    s = s.upper()
    result = 0
    prev = 0
    for char in reversed(s):
        curr = roman_map.get(char, 0)
        if curr < prev:
            result -= curr
        else:
            result += curr
        prev = curr
    return result