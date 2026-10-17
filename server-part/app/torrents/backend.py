"""
Интерфейс торрент-бэкенда и общие структуры данных.

Код приложения работает только через TorrentBackend.
Конкретная реализация (Transmission, libtorrent) подставляется
в manager.py в зависимости от settings.TORRENT_BACKEND.

Все методы синхронные. Вызывающий код при необходимости оборачивает
их в asyncio.to_thread.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


# ============================================================
# ОБЩИЕ СТРУКТУРЫ
# ============================================================

@dataclass
class TorrentInfo:
    """Нормализованное представление торрента в клиенте."""
    client_id: int              # id внутри клиента (Transmission, libtorrent)
    hash: str                   # info_hash в hex (lowercase)
    name: str                   # имя торрента
    status: str                 # 'downloading' | 'seeding' | 'paused' | 'error' | ...
    progress: float             # 0.0..1.0
    save_path: str              # куда клиент складывает файлы
    total_bytes: int
    downloaded_bytes: int
    rate_download: int          # байт/сек
    rate_upload: int            # байт/сек
    eta_sec: int | None
    peers: int
    seeds: int
    is_finished: bool
    error_message: str | None = None


@dataclass
class TorrentFileInfo:
    """Нормализованное представление файла внутри торрента."""
    index: int                  # индекс файла в торренте (для prioritize_files)
    name: str                   # путь внутри торрента (включая имя торрента)
    size_bytes: int
    downloaded_bytes: int
    progress: float             # 0.0..1.0
    is_complete: bool
    abs_path: Path | None = None  # абсолютный путь к файлу


# ============================================================
# ИНТЕРФЕЙС
# ============================================================

class TorrentBackend(Protocol):
    """
    Интерфейс торрент-клиента.

    Все методы синхронные — вызывающий код сам решает,
    оборачивать ли их в asyncio.to_thread.
    """

    # ---------- связь ----------

    def ping(self) -> bool:
        """Проверка доступности клиента."""
        ...

    # ---------- жизненный цикл ----------

    def add_magnet(self, magnet: str, save_path: Path) -> int | None:
        """
        Добавляет magnet-ссылку.
        Возвращает client_id торрента или None при ошибке.
        """
        ...

    def add_torrent_bytes(self, data: bytes, save_path: Path,
                      auto_priority: bool = True) -> int | None:
        """
        Добавляет торрент из .torrent-файла.
        При auto_priority=True — сразу выставляет приоритеты:
        high на первый аудио, normal на остальные, unwanted на не-аудио.
        """
        ...

    def remove(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """Удаляет торрент. Возвращает True при успехе."""
        ...

    # ---------- чтение ----------

    def get_torrent(self, torrent_hash: str) -> TorrentInfo | None:
        """Информация о торренте или None, если не найден."""
        ...

    def list_active(self) -> list[TorrentInfo]:
        """Все торренты в клиенте."""
        ...

    def get_files(self, torrent_hash: str) -> list[TorrentFileInfo]:
        """
        Файлы внутри торрента.
        Пустой список, если метаданные ещё не получены.
        """
        ...

    def get_file_path(self, torrent_hash: str,
                      file_index: int) -> Path | None:
        """Абсолютный путь к файлу внутри торрента."""
        ...

    # ---------- управление ----------

    def set_file_priorities(self, torrent_hash: str,
                            high: list[int],
                            normal: list[int],
                            low: list[int]) -> bool:
        """Приоритеты файлов внутри торрента (по индексам)."""
        ...

    def enable_sequential(self, torrent_hash: str,
                          enabled: bool = True) -> bool:
        """
        Последовательная загрузка кусков внутри файла.
        Поддерживается не всеми клиентами.
        """
        ...