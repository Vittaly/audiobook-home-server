"""
Раскладка файлов на диске. Единственное место, которое решает,
куда положить книгу или обложку.
"""

import hashlib
import logging
import shutil
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# символы, недопустимые в именах файлов на Windows
_FORBIDDEN = '<>:"/\\|?*'

# максимальная длина имени папки/файла
_MAX_NAME_LEN = 200


def sanitize_name(name: str) -> str:
    """
    Приводит произвольную строку к безопасному имени папки.
    Чистит запрещённые символы, схлопывает пробелы, обрезает длину.
    """
    if not name:
        return "unnamed"

    for ch in _FORBIDDEN:
        name = name.replace(ch, "_")

    # схлопываем пробелы и переносы
    name = " ".join(name.split())

    # запрещаем .. и . в начале/конце
    name = name.strip(". ")

    if len(name) > _MAX_NAME_LEN:
        name = name[:_MAX_NAME_LEN].rstrip(". ")

    return name or "unnamed"


def book_dir(torrent_name: str) -> Path:
    """
    Папка книги внутри downloads/.
    libtorrent создаст её сам при скачивании,
    мы только вычисляем путь.
    """
    return settings.downloads_dir / sanitize_name(torrent_name)


def book_rel(torrent_name: str) -> str:
    """Имя папки книги относительно downloads_dir."""
    return sanitize_name(torrent_name)


def abs_book(torrent_name: str) -> Path:
    """Абсолютный путь к папке книги."""
    return book_dir(torrent_name)


# ---------- обложки ----------

def cover_file(sha256: str, ext: str) -> Path:
    """Файл обложки в covers/ с шардированием по sha[:2]."""
    ext = _normalize_ext(ext)
    return settings.covers_dir / sha256[:2] / f"{sha256}.{ext}"


def cover_rel(sha256: str, ext: str) -> str:
    """Путь обложки относительно covers_dir — то, что хранится в БД."""
    ext = _normalize_ext(ext)
    return f"{sha256[:2]}/{sha256}.{ext}"


def abs_cover(rel: str) -> Path:
    """Собрать абсолютный путь из того, что хранится в БД."""
    return settings.covers_dir / rel


# ---------- хеши и запись ----------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write(target: Path, data: bytes) -> None:
    """
    Пишет файл через tmp + rename.
    Никогда не оставляет половинчатых файлов.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)

    tmp = settings.tmp_dir / f"{target.name}.part"
    tmp.write_bytes(data)
    tmp.replace(target)


def ensure_free_space(min_free_mb: int) -> bool:
    """Проверка перед добавлением торрента."""
    settings.ensure_dirs()
    return free_space_mb() >= min_free_mb


def free_space_mb() -> int:
    settings.ensure_dirs()
    return shutil.disk_usage(settings.downloads_dir).free // (1024 * 1024)


# ---------- удаление ----------

def remove_book_dir(torrent_name: str) -> None:
    """Удаляет папку книги. Идемпотентно."""
    path = book_dir(torrent_name)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        log.info("Удалена папка книги: %s", path)


def remove_cover(rel: str) -> None:
    """Удаляет файл обложки, если он существует."""
    if not rel:
        return
    path = abs_cover(rel)
    if path.exists():
        try:
            path.unlink()
            log.info("Удалена обложка: %s", path)
        except OSError:
            log.exception("Не удалось удалить обложку %s", path)


# ---------- внутреннее ----------

def _normalize_ext(ext: str) -> str:
    """'.jpeg' / 'JPEG' / 'jpeg' → 'jpeg'."""
    if not ext:
        return "jpg"
    return ext.lstrip(".").lower() or "jpg"