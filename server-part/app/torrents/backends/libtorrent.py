"""
Реализация TorrentBackend через libtorrent.

Пока заглушка: методы возвращают безопасные значения и логируют warning.
Включить: TORRENT_BACKEND=libtorrent в .env + установить python-libtorrent.
"""

import logging
from pathlib import Path

from app.torrents.backend import TorrentBackend, TorrentFileInfo, TorrentInfo

log = logging.getLogger(__name__)

_MSG = "libtorrent backend: %s ещё не реализовано"


class LibtorrentBackend(TorrentBackend):

    def __init__(self):
        log.info("LibtorrentBackend создан (заглушка)")
        try:
            import libtorrent as lt
            self._lt = lt
            log.info("libtorrent версия: %s", lt.__version__)
        except ImportError:
            self._lt = None
            log.warning("libtorrent не установлен, backend не будет работать")

    def ping(self) -> bool:
        return self._lt is not None

    def add_magnet(self, magnet: str, save_path: Path) -> int | None:
        log.warning(_MSG, "add_magnet")
        return None

    def remove(self, torrent_hash: str, delete_files: bool = True) -> bool:
        log.warning(_MSG, "remove")
        return False

    def get_torrent(self, torrent_hash: str) -> TorrentInfo | None:
        log.warning(_MSG, "get_torrent")
        return None

    def list_active(self) -> list[TorrentInfo]:
        log.warning(_MSG, "list_active")
        return []

    def get_files(self, torrent_hash: str) -> list[TorrentFileInfo]:
        log.warning(_MSG, "get_files")
        return []

    def get_file_path(self, torrent_hash: str,
                      file_index: int) -> Path | None:
        log.warning(_MSG, "get_file_path")
        return None

    def set_file_priorities(self, torrent_hash: str,
                            high: list[int],
                            normal: list[int],
                            low: list[int]) -> bool:
        log.warning(_MSG, "set_file_priorities")
        return False

    def enable_sequential(self, torrent_hash: str,
                          enabled: bool = True) -> bool:
        log.warning(_MSG, "enable_sequential")
        return False