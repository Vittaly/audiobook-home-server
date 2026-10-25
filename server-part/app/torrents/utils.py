"""
Утилиты для работы с файлами торрентов.

Общая логика сортировки и распознавания — используется и в manager
(для локального парсинга .torrent), и в backend (для выбора первого
аудиофайла после добавления торрента в клиент).

Единая точка правды: имя файла → порядок → первый аудио.
"""
from __future__ import annotations


import re
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_NUM_PREFIX_RE = re.compile(r"^[^\d]*(\d+)")


def is_audio(name: str) -> bool:
    return any(name.lower().endswith(e) for e in AUDIO_EXTS)


def is_image(name: str) -> bool:
    return any(name.lower().endswith(e) for e in IMAGE_EXTS)


def extract_prefix(name: str) -> int | None:
    """Числовой префикс в любом компоненте пути: '01.mp3' → 1."""
    for part in Path(name).parts:
        m = _NUM_PREFIX_RE.match(part)
        if m:
            return int(m.group(1))
    return None


def natural(name: str) -> list:
    """Natural sort: '2' < '10'."""
    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r"(\d+)", name)
    ]


def file_order_key(name: str) -> tuple:
    """
    Ключ сортировки файлов:
      1. Файлы с числовым префиксом — по возрастанию числа.
      2. Остальные — по natural sort имени.
    '01.mp3' всегда раньше 'Intro.mp3'.
    """
    prefix = extract_prefix(name)
    if prefix is not None:
        return (0, prefix, natural(name))
    return (1, 0, natural(name))


def find_first_audio(items, name_getter, index_getter) -> int | None:
    """
    Возвращает индекс первого аудиофайла в списке.
    items — список чего угодно, для чего заданы name_getter и index_getter.
    """
    audio = [
        (index_getter(x), name_getter(x))
        for x in items
        if is_audio(name_getter(x))
    ]
    if not audio:
        return None
    audio.sort(key=lambda x: file_order_key(x[1]))
    return audio[0][0]

def bdecode(data: bytes):
    """Минимальный bencode-парсер."""
    pos = 0

    def parse():
        nonlocal pos
        c = data[pos:pos + 1]
        if c == b"d":
            pos += 1
            d = {}
            while data[pos:pos + 1] != b"e":
                k = parse()
                v = parse()
                d[k] = v
            pos += 1
            return d
        if c == b"l":
            pos += 1
            lst = []
            while data[pos:pos + 1] != b"e":
                lst.append(parse())
            pos += 1
            return lst
        if c == b"i":
            pos += 1
            end = data.index(b"e", pos)
            n = int(data[pos:end])
            pos = end + 1
            return n
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        pos = colon + 1
        s = data[pos:pos + length]
        pos += length
        return s

    return parse()


def bencode(obj) -> bytes:
    """Bencode-энкодер, нужен для подсчёта sha1 от info."""
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, str):
        b = obj.encode()
        return str(len(b)).encode() + b":" + b
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = []
        for k in sorted(obj.keys()):
            items.append(bencode(k))
            items.append(bencode(obj[k]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError(f"bencode: не поддерживается {type(obj)}")