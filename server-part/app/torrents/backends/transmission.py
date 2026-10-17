"""
Реализация TorrentBackend через transmission-rpc.

Особенности transmission-rpc 7.x:
  - File имеет поля name, size, completed, priority, selected.
    Прогресса в процентах нет — считаем completed / size.
  - f.name — это путь **внутри торрента**, включая имя торрента
    для многофайловых раздач. Например:
      'Эрл Дерр Биггерс - Семь ключей [Иван Никонов]/01.mp3'
    Поэтому abs_path = t.download_dir / f.name,
    без повторного добавления t.name.
  - Torrent.status может возвращаться строкой ('downloading',
    'seeding', 'stopped', ...) или числом (0..6). Обрабатываем оба.
  - Прогресс торрента — в процентах (0..100), делим на 100.

Загрузка:
  - add_torrent_bytes — основной путь. Метаданные из .torrent-файла
    попадают в клиент мгновенно, независимо от DHT и трекеров.
  - add_magnet — fallback, если .torrent скачать не удалось.
"""

import logging
from pathlib import Path
import time

from transmission_rpc import Client
from transmission_rpc.error import (
    TransmissionConnectError, TransmissionError,
)

from app.config import settings
from app.torrents.backend import TorrentBackend, TorrentFileInfo, TorrentInfo
from app.torrents.utils import is_audio, is_image, file_order_key

log = logging.getLogger(__name__)


# Резервная карта на случай, если status придёт числом (старые версии).
_STATUS_MAP = {
    0: "stopped",
    1: "queued_check",
    2: "checking",
    3: "queued_download",
    4: "downloading",
    5: "queued_seed",
    6: "seeding",
}


class TransmissionBackend(TorrentBackend):

    def __init__(self):
        self._client: Client | None = None
        log.info("TransmissionBackend: %s:%d",
                 settings.TRANSMISSION_HOST, settings.TRANSMISSION_PORT)

    # ============================================================
    # СВЯЗЬ
    # ============================================================

    def _connect(self) -> Client:
        if self._client is None:
            self._client = Client(
                host=settings.TRANSMISSION_HOST,
                port=settings.TRANSMISSION_PORT,
                username=settings.TRANSMISSION_USER or None,
                password=settings.TRANSMISSION_PASSWORD or None,
                timeout=10,
            )
        return self._client

    def ping(self) -> bool:
        try:
            self._connect().get_session()
            return True
        except TransmissionConnectError as e:
            log.error("Transmission недоступен: %s", e)
            self._client = None
            return False
        except Exception:
            log.exception("Ошибка ping")
            self._client = None
            return False

    # ============================================================
    # ЖИЗНЕННЫЙ ЦИКЛ
    # ============================================================

    def add_magnet(self, magnet: str, save_path: Path) -> int | None:
        """
        Добавляет magnet-ссылку.
        Метаданные клиент будет запрашивать у пиров через BEP-9.
        Для магнитов с одним трекером это может занять часы.
        """
        try:
            t = self._connect().add_torrent(
                magnet, download_dir=str(save_path), paused=False)
            log.info("Transmission: добавлен magnet id=%s name=%s",
                     t.id, t.name)
            return t.id
        except TransmissionError as e:
            log.error("add_magnet: %s", e)
            return None
        except Exception:
            log.exception("add_magnet")
            return None

    def add_torrent_bytes(self, data: bytes, save_path: Path,
                      auto_priority: bool = True) -> int | None:
        """
        Добавляет торрент из байтов .torrent-файла.

        Если auto_priority=True — после добавления находит первый
        аудиофайл среди тех, что вернул клиент, и сразу выставляет:
        - high на него,
        - normal на остальные аудио,
        - unwanted на не-аудио.

        Индекс определяется по факту из Transmission, а не из локального
        .torrent — так исключается рассинхрон нумерации.
        """
        if not data:
            log.warning("add_torrent_bytes: пустые данные")
            return None

        try:
            t = self._connect().add_torrent(
                torrent=data,
                download_dir=str(save_path),
                paused=False,
            )
            log.info("Transmission: добавлен из файла id=%s name=%s",
                    t.id, t.name)
        except TransmissionError as e:
            log.error("add_torrent_bytes: %s", e)
            return None
        except Exception:
            log.exception("add_torrent_bytes")
            return None

        if auto_priority:
            # небольшая пауза, чтобы Transmission успел построить список файлов
            time.sleep(0.3)
            self._apply_initial_priorities(t)

        return t.id


    def _apply_initial_priorities(self, t) -> None:
        """
        Находит первый аудиофайл и выставляет приоритеты ОДНИМ вызовом
        change_torrent — только priority-*, без files_wanted/unwanted.

        Почему без files-*:
        Transmission обрабатывает files-* асинхронно. Если рядом
        отправить priority-*, операция с файлами может примениться
        ПОСЛЕ и сбросить high на normal или low. Убирая files-*,
        мы избавляемся от гонки.

        Не-аудио не исключаются, а получают priority_low. Они всё равно
        скачаются — но медленно и не мешают аудио.
        """
        try:
            files = t.get_files()
        except Exception:
            log.exception("_apply_initial_priorities: get_files упал")
            return

        if not files:
            log.warning("_apply_initial_priorities: файлы ещё не видны, "
                        "poller доделает")
            return

        audio = sorted(
            (f for f in files if is_audio(f.name)),
            key=lambda f: file_order_key(f.name),
        )
        if not audio:
            log.warning("_apply_initial_priorities: аудиофайлов нет")
            return

        first = audio[0]
        high = [first.id]
        # обложки томов должны скачаться — normal (не low)
        normal = [f.id for f in audio if f.id != first.id]
        normal += [f.id for f in files if is_image(f.name)]
        low = [f.id for f in files
               if not is_audio(f.name) and not is_image(f.name)]

        log.info(
            "_apply_initial_priorities: hash=%s файлов=%d "
            "high=[%d] normal=%d low=%d",
            t.hash_string[:8], len(files), first.id, len(normal), len(low),
        )
        log.info(
            "_apply_initial_priorities: первый файл id=%s name=%r",
            first.id, first.name[:70],
        )

        try:
            self._connect().change_torrent(
                t.id,
                priority_high=high,
                priority_normal=normal,
                priority_low=low,
            )
            log.info("_apply_initial_priorities: change_torrent отправлен")
        except Exception:
            log.exception("_apply_initial_priorities: change_torrent упал")
            return

        # читаем обратно, что применилось
        try:
            time.sleep(0.3)
            after = t.get_files()
            for f in sorted(after, key=lambda x: x.id)[:6]:
                log.info(
                    "  после: id=%s priority=%+d name=%r",
                    f.id, f.priority, f.name[:60],
                )
        except Exception:
            log.exception("_apply_initial_priorities: чтение обратно упало")


    def _apply_initial_priorities(self, t, first_file_index: int) -> None:
        """
        Выставляет high на первый файл, normal на остальные аудио,
        unwanted на не-аудио.

        Вызывается сразу после добавления торрента — файлы уже видны,
        потому что метаданные пришли из .torrent-файла.
        """
        try:
            files = t.get_files()
        except Exception:
            log.exception("_apply_initial_priorities: get_files упал")
            return

        if not files:
            log.warning("_apply_initial_priorities: файлы ещё не видны, "
                        "poller доделает")
            return

        audio_ids = [f.id for f in files if is_audio(f.name)]
        # обложки томов должны скачаться — normal, не unwanted
        image_ids = [f.id for f in files if is_image(f.name)]
        non_audio_ids = [f.id for f in files
                         if not is_audio(f.name) and not is_image(f.name)]

        if first_file_index not in audio_ids:
            log.warning("_apply_initial_priorities: file_index=%s не аудио "
                        "(аудио: %s)", first_file_index, audio_ids[:5])
            return

        high = [first_file_index]
        normal = [i for i in audio_ids if i != first_file_index]
        normal += image_ids
        unwanted = non_audio_ids

        kwargs = {
            "priority_high": high,
            "priority_normal": normal,
        }
        if unwanted:
            kwargs["files_unwanted"] = unwanted
            kwargs["files_wanted"] = high + normal

        try:
            self._connect().change_torrent(t.id, **kwargs)
            log.info(
                "Transmission: стартовые приоритеты %s "
                "high=%s normal=%d unwanted=%d",
                t.hash_string[:8], high, len(normal), len(unwanted),
            )
        except Exception:
            log.exception("_apply_initial_priorities: change_torrent упал")

        def remove(self, torrent_hash: str, delete_files: bool = True) -> bool:
            try:
                t = self._find_by_hash(torrent_hash)
                if t is None:
                    log.warning("Transmission: торрент %s не найден",
                                torrent_hash[:8])
                    return False
                self._connect().remove_torrent(t.id, delete_data=delete_files)
                log.info("Transmission: удалён id=%s (data=%s)",
                        t.id, delete_files)
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
        try:
            return [self._to_info(t) for t in self._connect().get_torrents()]
        except Exception:
            log.exception("list_active")
            return []

    def get_files(self, torrent_hash: str) -> list[TorrentFileInfo]:
        """
        Файлы внутри торрента.

        transmission-rpc File:
          - f.id        — индекс файла в торренте (int)
          - f.name      — путь внутри торрента, включая имя торрента
                          для многофайловых раздач
          - f.size      — размер в байтах (int)
          - f.completed — сколько байт скачано (int)
          - f.priority  — приоритет
          - f.selected  — выбран ли файл для загрузки

        Прогресса в процентах нет — считаем completed / size.
        Абсолютный путь = t.download_dir / f.name.
        """
        t = self._find_by_hash(torrent_hash)
        if t is None:
            return []

        try:
            files = t.get_files()
        except Exception:
            log.exception("get_files(%s)", torrent_hash[:8])
            return []

        download_dir = Path(t.download_dir)
        result: list[TorrentFileInfo] = []

        for f in files:
            size = int(f.size or 0)
            completed = int(f.completed or 0)

            if size > 0:
                progress = min(1.0, completed / size)
            else:
                progress = 0.0

            is_complete = size > 0 and completed >= size

            # f.name уже включает папку торрента — не дублируем
            abs_path = download_dir / f.name

            result.append(TorrentFileInfo(
                index=int(f.id),
                name=str(f.name),
                size_bytes=size,
                downloaded_bytes=completed,
                progress=progress,
                is_complete=is_complete,
                abs_path=abs_path,
            ))

        return result

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
        t = self._find_by_hash(torrent_hash)
        if t is None:
            return False
        try:
            self._connect().change_torrent(
                t.id,
                priority_high=high,
                priority_normal=normal,
                priority_low=low,
            )
            log.info("Transmission: приоритеты %s high=%s normal=%d low=%d",
                    torrent_hash[:8], high, len(normal), len(low))
            return True
        except Exception:
            log.exception("set_file_priorities(%s)", torrent_hash[:8])
            return False

    def enable_sequential(self, torrent_hash: str,
                          enabled: bool = True) -> bool:
        t = self._find_by_hash(torrent_hash)
        if t is None:
            return False
        try:
            self._connect().change_torrent(t.id, sequential_download=enabled)
            log.info("Transmission: sequential %s для %s",
                     "on" if enabled else "off", torrent_hash[:8])
            return True
        except AttributeError:
            log.warning("Transmission не поддерживает sequential_download "
                        "(нужна версия 4.1+)")
            return False
        except Exception:
            log.exception("enable_sequential(%s)", torrent_hash[:8])
            return False

    # ============================================================
    # ВНУТРЕННЕЕ
    # ============================================================

    def _find_by_hash(self, torrent_hash: str):
        try:
            h = torrent_hash.lower()
            for t in self._connect().get_torrents():
                if t.hash_string.lower() == h:
                    return t
        except Exception:
            log.exception("_find_by_hash(%s)", torrent_hash[:8])
        return None

    @staticmethod
    def _to_info(t) -> TorrentInfo:
        # status может приходить строкой ('downloading', 'seeding', ...)
        # или числом (0..6) в зависимости от версии transmission-rpc.
        status = t.status
        if isinstance(status, int):
            status = _STATUS_MAP.get(status, f"unknown({status})")

        # progress в transmission-rpc — проценты (0..100)
        progress = float(t.progress or 0) / 100.0

        total_bytes = int(t.total_size or 0)
        downloaded_bytes = int(total_bytes * progress)

        return TorrentInfo(
            client_id=int(t.id),
            hash=t.hash_string.lower(),
            name=t.name,
            status=status,
            progress=progress,
            save_path=t.download_dir,
            total_bytes=total_bytes,
            downloaded_bytes=downloaded_bytes,
            rate_download=int(t.rate_download or 0),
            rate_upload=int(t.rate_upload or 0),
            eta_sec=int(t.eta.total_seconds()) if t.eta else None,
            peers=int(t.peers_connected or 0),
            seeds=int(t.peers_sending_to_us or 0),
            is_finished=(progress >= 1.0),
        )