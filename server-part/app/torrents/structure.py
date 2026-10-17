"""
Разбор структуры торрента на отдельные книги.

Правило: первый компонент rel_path — папка торрента (её создаёт libtorrent).
Всё, что глубже — структура самой раздачи.

Если файлы лежат плоско — торрент содержит одну книгу.
Если есть вложенные папки — каждая папка = отдельная книга.

Из названия папки книги дополнительно извлекаются:
  - "volume"      — номер тома/книги ('Том 2', 'Книга 1', 'Т.3', 'Часть 4');
  - "series_hint" — название серии, если папка построена как
                    «Название серии. Том N» (иначе None — серия берётся
                    из карточки раздачи).
"""

import logging
import re
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# «Том 2», «Книга 1», «Т.3», «Часть 4», «Кн. 5»
_TOME_RE = re.compile(
    r"(?:^|[\s.,:;_\-–—])(?:том|книга|часть|выпуск|т\.|кн\.|в\.)"
    r"\s*[№#]?\s*(\d+)",
    re.IGNORECASE,
)
# название серии перед «Том N»: «Хроники Амбера. Том 2» → «Хроники Амбера»
_SERIES_BEFORE_TOME_RE = re.compile(
    r"^(.*?)[\s.,:;_\-–—]*(?:том|книга|часть|выпуск|т\.|кн\.|в\.)"
    r"\s*[№#]?\s*\d+",
    re.IGNORECASE,
)
# «1. Название», «01. Название», «1) Название» — префикс-номер без слова «том»
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[.)\-–—]?\s*")


def split_torrent_into_books(files: list[dict],
                             torrent_name: str) -> list[dict]:
    """
    files: список файлов торрента, каждый с полями:
        index, rel_path, size_bytes, file_offset

    Возвращает список книг:
        [{"title", "folder", "audio": [...], "cover", "size_bytes",
          "volume", "series_hint"}, ...]

    volume / series_hint — None, если уверенно извлечь не удалось.
    """
    trimmed = _trim_torrent_root(files)
    depths = {len(f["sub_path"].parts) for f in trimmed}

    if depths == {1}:
        audio = [f for f in trimmed if _is_audio(f)]
        if not audio:
            log.warning("Торрент %s не содержит аудио", torrent_name)
            return []
        return [{
            "title": None,
            "folder": None,
            "audio": _normalize_audio(audio),
            "cover": _pick_cover(trimmed),
            "size_bytes": sum(f["size_bytes"] for f in audio),
            "volume": None,
            "series_hint": None,
        }]

    groups: dict[str, list[dict]] = defaultdict(list)
    for f in trimmed:
        groups[f["sub_path"].parts[0]].append(f)

    books = []
    for folder, folder_files in sorted(groups.items()):
        audio = [f for f in folder_files if _is_audio(f)]
        if not audio:
            continue
        clean, volume, series_hint = _analyze_folder(folder)
        books.append({
            "title": clean,
            "folder": folder,
            "audio": _normalize_audio(audio),
            "cover": _pick_cover(folder_files),
            "size_bytes": sum(f["size_bytes"] for f in audio),
            "volume": volume,
            "series_hint": series_hint,
        })

    if len(books) == 1:
        books[0]["folder"] = None
        books[0]["title"] = None
        # для единственной книги хинты не нужны — серия смотрится по карточке
        books[0]["volume"] = None
        books[0]["series_hint"] = None

    return books


def _trim_torrent_root(files: list[dict]) -> list[dict]:
    result = []
    for f in files:
        parts = Path(f["rel_path"]).parts
        sub = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
        result.append({**f, "sub_path": sub})
    return result


def _normalize_audio(audio: list[dict]) -> list[dict]:
    audio_sorted = sorted(audio, key=lambda f: _natural(f["sub_path"].name))
    return [
        {
            "index": f["index"],
            "rel_path": f["rel_path"],
            "size_bytes": f["size_bytes"],
            "file_offset": f.get("file_offset", 0),
        }
        for f in audio_sorted
    ]


def _is_audio(f: dict) -> bool:
    return Path(f["sub_path"].name).suffix.lower() in AUDIO_EXTS


def _is_image(f: dict) -> bool:
    return Path(f["sub_path"].name).suffix.lower() in IMAGE_EXTS


def _natural(name: str) -> list:
    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r"(\d+)", name)
    ]


def _pick_cover(files: list[dict]) -> str | None:
    images = [f for f in files if _is_image(f)]
    if not images:
        return None
    priority = ("cover", "folder", "front", "обложка")
    for p in priority:
        for f in images:
            if p in Path(f["sub_path"].name).stem.lower():
                return f["rel_path"]
    return images[0]["rel_path"]


def _clean_folder_name(folder: str) -> str:
    s = re.sub(r"^\s*(?:книга\s*)?\d+\s*[.)\-–—]?\s*", "",
               folder, flags=re.I)
    return s.strip(" .-–—") or folder


def _analyze_folder(folder: str) -> tuple[str, int | None, str | None]:
    """
    Разбирает название папки книги:
      - «Хроники Амбера. Том 2»  → ('Хроники Амбера', 2, 'Хроники Амбера')
      - «Том 1. Наппадающий»     → ('Наппадающий', 1, None)
      - «Книга 3»                → (folder, 3, None)
      - «1. Название»            → ('Название', 1, None)
      - «Просто название»        → ('Просто название', None, None)

    Возвращает (clean_title, volume, series_hint).
    """
    m = _TOME_RE.search(folder)
    volume = int(m.group(1)) if m else None

    series_hint: str | None = None
    clean = folder

    if volume is not None:
        # название серии перед «Том N»
        sm = _SERIES_BEFORE_TOME_RE.match(folder)
        head = sm.group(1).strip(" .-–—,;:") if sm else ""
        if head and len(head) >= 2 and \
                not re.fullmatch(r"[\d\W]+", head, flags=re.UNICODE):
            series_hint = head
            clean = folder[len(head):].lstrip(" .-–—,;:")
            clean = _strip_tome_prefix(clean)
        else:
            # «Том N» в середине/конце: «Название — Том 2»
            prefix = _strip_tome_prefix(folder)
            prefix = _LEADING_NUM_RE.sub("", prefix)
            clean = prefix.strip(" .-–—,;:") or folder
    else:
        # без явного «тома» — просто убираем ведущий номер
        clean = _LEADING_NUM_RE.sub("", folder).strip(" .-–—,;:") or folder

    if not clean or len(clean) < 2:
        clean = folder
    return clean, volume, series_hint


def _strip_tome_prefix(text: str) -> str:
    """Убирает ведущий 'Том 1' / 'Книга 2.' и т.п."""
    s = re.sub(r"^\s*(?:том|книга|часть|выпуск|т\.|кн\.|в\.)"
               r"\s*[№#]?\s*\d+\s*[.:,;\-–—]?\s*",
               "", text, flags=re.IGNORECASE)
    s = _LEADING_NUM_RE.sub("", s)
    return s.strip()
