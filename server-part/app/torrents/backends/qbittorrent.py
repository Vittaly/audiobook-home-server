"""
Реализация TorrentBackend через qbittorrent-api.

Ключевые моменты qBittorrent Web API:
  - Файлы: index, name, size, progress (0.0..1.0),
    priority (0=skip, 1=normal, 6=high, 7=max), is_seed.
  - Приоритеты задаются вызовом torrents_file_priority() на список
    file_ids с одним значением priority — поэтому три вызова.
  - sequential_download — это toggle, а не set. Проверяем текущее
    состояние seq_dl, вызываем toggle только при расхождении.
  - app.version — это @property, а не метод. Вызывать без скобок.
  - torrents_add возвращает 'Ok.' или бросает Conflict409Error,
    если торрент с таким info-hash уже есть. Поэтому мы сначала
    проверяем наличие по хэшу, потом добавляем.
  - sequential_download передаём ОТДЕЛЬНО после приоритетов, а не
    в torrents_add — иначе qBittorrent может сбросить приоритеты.
"""

import hashlib
import logging
import re
import time
from pathlib import Path

import qbittorrentapi
from qbittorrentapi.exceptions import Conflict409Error

from app.config import settings
from app.torrents.backend import TorrentBackend, TorrentFileInfo, TorrentInfo
from app.torrents.utils import (
    is_audio, is_image, file_order_key, bdecode, bencode,
)

log = logging.getLogger(__name__)


# Значения приоритетов qBittorrent для файлов
_PRIO_SKIP = 0
_PRIO_NORMAL = 1
_PRIO_HIGH = 6
_PRIO_MAX = 7

_MAGNET_HASH_RE = re.compile(r"btih:([0-9a-fA-F]{40})")


class QBittorrentBackend(TorrentBackend):

    def __init__(self):
        self._client: qbittorrentapi.Client | None = None
        log.info("QBittorrentBackend: %s:%d",
                 settings.QBITTORRENT_HOST, settings.QBITTORRENT_PORT)

    # ============================================================
    # СВЯЗЬ
    # ============================================================

    def _connect(self) -> qbittorrentapi.Client:
        if self._client is None:
            self._client = qbittorrentapi.Client(
                host=settings.QBITTORRENT_HOST,
                port=settings.QBITTORRENT_PORT,
                username=settings.QBITTORRENT_USER or None,
                password=settings.QBITTORRENT_PASSWORD or None,
            )
        return self._client

    def ping(self) -> bool:
        try:
            # app.version — свойство, без скобок
            v = self._connect().app.version
            log.debug("qBittorrent версия: %s", v)
            return True
        except qbittorrentapi.APIError as e:
            log.error("qBittorrent недоступен: %s", e)
            self._client = None
            return False
        except Exception:
            log.exception("Ошибка ping")
            self._client = None
            return False

    # ============================================================
    # ЖИЗНЕННЫЙ ЦИКЛ
    # ============================================================

    def add_magnet(self, magnet: str, save_path: Path) -> str | None:
        """
        Добавляет magnet.
        Перед torrents_add проверяем по info-hash, нет ли такого
        торрента в клиенте — если есть, возвращаем его хэш без
        обращения к qBittorrent.
        """
        torrent_hash = self._hash_from_magnet(magnet)

        if torrent_hash:
            existing = self._find_by_hash(torrent_hash)
            if existing is not None:
                log.info("add_magnet: торрент %s уже в клиенте",
                         torrent_hash[:8])
                return torrent_hash

        for attempt in (1, 2):
            try:
                c = self._connect()
                result = c.torrents_add(
                    urls=magnet,
                    save_path=str(save_path),
                    is_paused=False,
                )
                log.info("qBittorrent: torrents_add magnet → %r", result)

                if result != "Ok.":
                    log.warning("qBittorrent: torrents_add вернул %r", result)
                    if torrent_hash:
                        existing = self._find_by_hash(torrent_hash)
                        if existing is not None:
                            return torrent_hash
                    return None

                return self._latest_added_hash()

            except Conflict409Error:
                log.warning("add_magnet: 409, торрент уже есть")
                if torrent_hash:
                    return torrent_hash
                return self._hash_from_magnet(magnet)

            except Exception:
                log.exception("add_magnet (попытка %d)", attempt)
                self._client = None  # сброс сессии и повторная попытка
        return None

    def add_torrent_bytes(self, data: bytes, save_path: Path,
                          auto_priority: bool = True) -> str | None:
        """
        Добавляет торрент из байтов .torrent-файла.

        Если торрент уже есть по info-hash — не дёргаем torrents_add,
        сразу применяем приоритеты и возвращаем хэш. Это устраняет
        и 409, и мусорные сообщения в UI о повторном добавлении.
        """
        if not data:
            log.warning("add_torrent_bytes: пустые данные")
            return None

        torrent_hash = self._hash_from_torrent_bytes(data)
        log.info("add_torrent_bytes: info-hash=%s",
                 torrent_hash[:8] if torrent_hash else "?")

        # --- уже есть в клиенте? ---
        if torrent_hash:
            existing = self._find_by_hash(torrent_hash)
            if existing is not None:
                log.info("add_torrent_bytes: торрент %s уже в клиенте",
                         torrent_hash[:8])
                if auto_priority:
                    self._apply_initial_priorities(torrent_hash)
                return torrent_hash

        # --- добавляем ---
        for attempt in (1, 2):
            try:
                c = self._connect()
                result = c.torrents_add(
                    torrent_files=data,
                    save_path=str(save_path),
                    is_paused=False,
                )
                log.info("qBittorrent: torrents_add bytes → %r", result)

                if result != "Ok.":
                    log.warning("qBittorrent: torrents_add вернул %r", result)
                    if torrent_hash:
                        existing = self._find_by_hash(torrent_hash)
                        if existing is not None:
                            if auto_priority:
                                self._apply_initial_priorities(torrent_hash)
                            return torrent_hash
                    return None

            except Conflict409Error:
                log.warning("add_torrent_bytes: 409, торрент уже есть")
                if torrent_hash:
                    existing = self._find_by_hash(torrent_hash)
                    if existing is not None and auto_priority:
                        self._apply_initial_priorities(torrent_hash)
                    return torrent_hash
                return None

            except Exception:
                log.exception("add_torrent_bytes (попытка %d)", attempt)
                self._client = None  # сброс сессии и повторная попытка

        # --- успешно добавлен ---
        if torrent_hash is None:
            torrent_hash = self._latest_added_hash()
        if torrent_hash is None:
            log.warning("add_torrent_bytes: hash не определён")
            return None

        if auto_priority:
            time.sleep(0.5)  # ждём, пока qBittorrent построит список файлов
            self._apply_initial_priorities(torrent_hash)

        return torrent_hash

    def remove(self, torrent_hash: str, delete_files: bool = True) -> bool:
        try:
            self._connect().torrents_delete(
                delete_files=delete_files,
                torrent_hashes=torrent_hash,
            )
            log.info("qBittorrent: удалён %s (data=%s)",
                     torrent_hash[:8], delete_files)
            return True
        except Exception:
            log.exception("remove(%s)", torrent_hash[:8])
            return False

    # ============================================================
    # ЧТЕНИЕ
    # ============================================================

    def get_torrent(self, torrent_hash: str) -> TorrentInfo | None:
        t = self._find_by_hash(torrent_hash)
        if t is None:
            return None
        return self._to_info(t)

    def list_active(self) -> list[TorrentInfo]:
        for attempt in (1, 2):
            try:
                return [self._to_info(t)
                        for t in self._connect().torrents_info()]
            except Exception:
                log.exception("list_active (попытка %d)", attempt)
                self._client = None
        return []

    def get_files(self, torrent_hash: str) -> list[TorrentFileInfo]:
        """Файлы внутри торрента."""
        try:
            t = self._find_by_hash(torrent_hash)
            if t is None:
                return []

            files_raw = self._connect().torrents_files(
                torrent_hash=torrent_hash)
            save_path = Path(t.save_path)

            result: list[TorrentFileInfo] = []
            for f in files_raw:
                size = int(f.size or 0)
                progress = float(f.progress or 0.0)
                downloaded = int(size * progress)
                is_complete = progress >= 1.0

                abs_path = save_path / f.name

                result.append(TorrentFileInfo(
                    index=int(f.index),
                    name=str(f.name),
                    size_bytes=size,
                    downloaded_bytes=downloaded,
                    progress=progress,
                    is_complete=is_complete,
                    abs_path=abs_path,
                ))
            return result
        except Exception:
            log.exception("get_files(%s)", torrent_hash[:8])
            return []

    def get_file_path(self, torrent_hash: str,
                      file_index: int) -> Path | None:
        for f in self.get_files(torrent_hash):
            if f.index == file_index:
                return f.abs_path
        return None

    # ============================================================
    # УПРАВЛЕНИЕ
    # ============================================================

    def set_file_priorities(self, torrent_hash: str,
                            high: list[int],
                            normal: list[int],
                            low: list[int]) -> bool:
        """
        Выставляет приоритеты файлов.
        qBittorrent принимает один priority на список file_ids,
        поэтому три вызова. low трактуем как skip (0).
        """
        try:
            c = self._connect()

            log.info("qBittorrent set_file_priorities: hash=%s "
                     "high=%s normal=%d low=%d",
                     torrent_hash[:8], high, len(normal), len(low))

            if high:
                c.torrents_file_priority(
                    torrent_hash=torrent_hash,
                    file_ids=high,
                    priority=_PRIO_HIGH,
                )
                log.info("  priority_high отправлен для %s", high)

            if normal:
                c.torrents_file_priority(
                    torrent_hash=torrent_hash,
                    file_ids=normal,
                    priority=_PRIO_NORMAL,
                )
                log.info("  priority_normal отправлен для %d файлов",
                         len(normal))

            if low:
                c.torrents_file_priority(
                    torrent_hash=torrent_hash,
                    file_ids=low,
                    priority=_PRIO_SKIP,
                )
                log.info("  priority_skip отправлен для %d файлов",
                         len(low))

            return True

        except Exception:
            log.exception("set_file_priorities(%s)", torrent_hash[:8])
            return False

    def enable_sequential(self, torrent_hash: str,
                          enabled: bool = True) -> bool:
        """
        Включает sequential download.
        В qBittorrent это toggle — проверяем seq_dl и вызываем
        только при расхождении.
        """
        try:
            t = self._find_by_hash(torrent_hash)
            if t is None:
                return False

            current = bool(getattr(t, "seq_dl", False))
            if current == enabled:
                log.debug("qBittorrent: sequential уже %s у %s",
                          "on" if enabled else "off", torrent_hash[:8])
                return True

            self._connect().torrents_toggle_sequential_download(
                torrent_hashes=torrent_hash)
            log.info("qBittorrent: sequential переключён в %s у %s",
                     "on" if enabled else "off", torrent_hash[:8])
            return True
        except Exception:
            log.exception("enable_sequential(%s)", torrent_hash[:8])
            return False

    # ============================================================
    # ПЕРВИЧНЫЕ ПРИОРИТЕТЫ
    # ============================================================

    def _apply_initial_priorities(self, torrent_hash: str) -> None:
        """
        Фаза 1: находит первый аудиофайл и выставляет приоритеты:
          high (6) на первый аудио,
          normal (1) на обложки томов (лёгкие, нужны для своей обложки),
          skip (0) на ВСЕ остальные аудио и прочий не-аудио мусор.

        Обложки (картинки) в skip НЕ попадают — иначе high→normal→skip
        перезапишет их обратно в skip и обложка тома не скачается.

        Качаем ровно одну (стартовую) главу за раз: сервер не тратит пиров
        и полосу на остальные файлы, поэтому первая глава становится готовой
        максимально быстро. Как только она готова, poller (или эндпоинт
        запроса главы) вызывает actualize_release, который переводит раздачу
        в фазу 2 — качается вся раздача, high на первую незагруженную главу.

        Sequential выключен — порядок файлов раздачи не равен аудио-порядку
        глав, а sequential игнорирует приоритеты.
        """
        files = self.get_files(torrent_hash)
        if not files:
            log.warning("_apply_initial_priorities: файлы ещё не видны "
                        "у %s, poller доделает", torrent_hash[:8])
            return

        audio = sorted(
            (f for f in files if is_audio(f.name)),
            key=lambda f: file_order_key(f.name),
        )
        if not audio:
            log.warning("_apply_initial_priorities: аудиофайлов нет у %s",
                        torrent_hash[:8])
            return

        first = audio[0]
        high = [first.index]
        normal = [f.index for f in files if is_image(f.name)]
        # ВСЕ прочие аудио + не-аудио мусор — в skip: качаем строго по одной
        # главе. Обложки (картинки) НЕ трогаем — они уже в normal выше, иначе
        # set_file_priorities перезапишет их в skip (порядок high→normal→skip)
        # и обложка тома не скачается.
        normal_set = set(normal)
        skip = [f.index for f in files if f.index != first.index
                and f.index not in normal_set]

        log.info("_apply_initial_priorities: hash=%s файлов=%d, "
                 "первый аудио index=%s name=%r",
                 torrent_hash[:8], len(files), first.index, first.name[:70])

        # 1. Приоритеты
        ok = self.set_file_priorities(
            torrent_hash, high=high, normal=normal, low=skip)
        if not ok:
            log.warning("_apply_initial_priorities: приоритеты не легли")

        # 2. Sequential ОТКЛЮЧЁН: порядок файлов раздачи не равен аудио-
        #    порядку глав, а sequential игнорирует приоритеты — из-за него
        #    стартовая глава скачивалась последней. Вместо sequential
        #    порядок задают приоритеты (high на первый аудиофайл).
        self.enable_sequential(torrent_hash, enabled=False)

        # 3. Читаем обратно, что получилось
        try:
            time.sleep(0.3)
            after = self.get_files(torrent_hash)
            log.info("_apply_initial_priorities: проверка после")
            for f in after[:8]:
                log.info("  index=%s progress=%.1f%% name=%r",
                         f.index, f.progress * 100, f.name[:60])
        except Exception:
            log.exception("_apply_initial_priorities: чтение упало")

    # ============================================================
    # ВНУТРЕННЕЕ
    # ============================================================

    def _find_by_hash(self, torrent_hash: str):
        h = torrent_hash.lower()
        for attempt in (1, 2):
            try:
                for t in self._connect().torrents_info():
                    if t.hash.lower() == h:
                        return t
            except Exception:
                log.exception("_find_by_hash(%s) (попытка %d)",
                              torrent_hash[:8], attempt)
                self._client = None  # сброс сессии и повторная попытка
        return None

    def _latest_added_hash(self) -> str | None:
        """Хэш самого свежего добавленного торрента."""
        for attempt in (1, 2):
            try:
                torrents = self._connect().torrents_info()
                if not torrents:
                    return None
                latest = max(torrents, key=lambda t: t.added_on or 0)
                return latest.hash
            except Exception:
                log.exception("_latest_added_hash (попытка %d)", attempt)
                self._client = None
        return None

    def _hash_from_magnet(self, magnet: str) -> str | None:
        m = _MAGNET_HASH_RE.search(magnet)
        return m.group(1).lower() if m else None

    def _hash_from_torrent_bytes(self, data: bytes) -> str | None:
        """sha1 от канонично закодированного словаря info."""
        try:
            info = bdecode(data)
            info_dict = info.get(b"info")
            if not isinstance(info_dict, dict):
                return None
            info_bencoded = bencode(info_dict)
            return hashlib.sha1(info_bencoded).hexdigest()
        except Exception:
            log.exception("_hash_from_torrent_bytes")
            return None

    @staticmethod
    def _to_info(t) -> TorrentInfo:
        progress = float(t.progress or 0.0)
        total_bytes = int(t.total_size or t.size or 0)
        downloaded_bytes = int(t.downloaded or 0)

        eta_raw = getattr(t, "eta", None)
        if eta_raw is None or eta_raw >= 8640000:
            eta_sec = None
        else:
            eta_sec = int(eta_raw)

        return TorrentInfo(
            client_id=t.hash,
            hash=t.hash.lower(),
            name=t.name,
            status=str(t.state),
            progress=progress,
            save_path=str(t.save_path),
            total_bytes=total_bytes,
            downloaded_bytes=downloaded_bytes,
            rate_download=int(t.dlspeed or 0),
            rate_upload=int(t.upspeed or 0),
            eta_sec=eta_sec,
            peers=int(t.num_leechs or 0),
            seeds=int(t.num_seeds or 0),
            is_finished=(progress >= 1.0),
        )