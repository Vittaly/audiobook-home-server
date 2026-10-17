"""
Менеджер торрентов.

Единая точка входа для всего кода приложения.
Выбирает backend по settings.TORRENT_BACKEND, предоставляет
высокоуровневые функции.

Загрузка книги:
  1. Скачиваем .torrent-файл с rutor по URL раздачи.
  2. Передаём байты в клиент с auto_priority=True — backend
     сам найдёт первый аудиофайл по фактическому списку файлов
     из клиента и выставит приоритеты.
  3. Если .torrent недоступен — fallback на magnet с обогащением
     трекерами. Для магнита файлы появляются не сразу, поэтому
     ждём их в фоновом потоке.

Приоритеты продвигаются дальше автоматически: poller смотрит на
status глав и, когда первая становится ready, ставит high на
следующую незавершённую.
"""

import logging
import re
import threading
import time
from pathlib import Path

import httpx

from app.config import settings
from app.torrents.daemon import resolve_mode
from app.torrents.backend import (
    TorrentBackend, TorrentFileInfo, TorrentInfo,
)
from app.torrents.utils import is_audio, file_order_key

log = logging.getLogger(__name__)


# ============================================================
# ИНИЦИАЛИЗАЦИЯ BACKEND'А
# ============================================================

_backend: TorrentBackend | None = None


def _build_backend() -> TorrentBackend:
    # Единый источник истины о режиме — daemon.resolve_mode().
    kind = resolve_mode()

    if kind == "qbittorrent":
        from app.torrents.backends.qbittorrent import QBittorrentBackend
        log.info("Торрент-бэкенд: qbittorrent")
        return QBittorrentBackend()

    if kind == "transmission":
        from app.torrents.backends.transmission import TransmissionBackend
        log.info("Торрент-бэкенд: transmission")
        return TransmissionBackend()

    from app.torrents.backends.libtorrent import LibtorrentBackend
    log.info("Торрент-бэкенд: libtorrent")
    return LibtorrentBackend()


def get_backend() -> TorrentBackend:
    global _backend
    if _backend is None:
        _backend = _build_backend()
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None


def backend_name() -> str:
    return type(get_backend()).__name__


# ============================================================
# ОБОГАЩЕНИЕ MAGNET (FALLBACK)
# ============================================================

_EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
]


def _enrich_magnet(magnet: str) -> str:
    for tr in _EXTRA_TRACKERS:
        enc = tr.replace(":", "%3A").replace("/", "%2F")
        if enc not in magnet:
            magnet += f"&tr={enc}"
    return magnet


# ============================================================
# ЗАГРУЗКА .TORRENT-ФАЙЛА
# ============================================================

_TORRENT_ID_RE = re.compile(r"/torrent/(\d+)")
_RUTOR_DL_URL = "https://d.rutor.info/download/{torrent_id}"


async def fetch_torrent_bytes(source_url: str | None) -> bytes | None:
    """
    Скачивает .torrent-файл с rutor по URL страницы раздачи.
    Извлекает ID раздачи и идёт на d.rutor.info.
    """
    if not source_url:
        return None

    m = _TORRENT_ID_RE.search(source_url)
    if not m:
        log.warning("fetch_torrent_bytes: нет torrent_id в %r", source_url)
        return None

    torrent_id = m.group(1)
    url = _RUTOR_DL_URL.format(torrent_id=torrent_id)
    log.info("fetch_torrent_bytes: GET %s", url)

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": source_url,
                "Accept": "application/x-bittorrent, */*",
            },
        ) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        log.warning("fetch_torrent_bytes: сеть — %s", e)
        return None

    if r.status_code != 200:
        log.warning("fetch_torrent_bytes: HTTP %d", r.status_code)
        return None

    data = r.content
    if not data or len(data) < 50:
        log.warning("fetch_torrent_bytes: короткий ответ (%d байт)",
                    len(data))
        return None

    if not data.startswith(b"d"):
        log.warning("fetch_torrent_bytes: ответ не торрент-файл, "
                    "первые байты %r", data[:40])
        return None

    log.info("fetch_torrent_bytes: получено %d байт", len(data))
    return data


# ============================================================
# ВЫСОКОУРОВНЕВЫЙ ЗАПУСК ЗАГРУЗКИ
# ============================================================

async def start_download(release, save_path: Path) -> int | None:
    """
    Запускает загрузку книги из release.

    Для .torrent-файла приоритеты ставит backend сразу после
    добавления, по фактическому списку файлов из клиента.
    Для magnet — фоновый поток ждёт появления файлов.
    """
    # --- 1. .torrent-файл ---
    torrent_bytes = await fetch_torrent_bytes(release.source_url)
    if torrent_bytes:
        client_id = add_torrent_bytes(
            torrent_bytes, save_path,
            auto_priority=True,
        )
        if client_id is not None:
            log.info("start_download: добавлен из .torrent "
                     "(client_id=%s)", client_id)
            return client_id

        log.warning("start_download: клиент отказал в .torrent, "
                    "пробуем magnet")

    # --- 2. magnet как fallback ---
    if not release.magnet:
        log.error("start_download: нет ни .torrent, ни magnet")
        return None

    client_id = add_magnet(release.magnet, save_path)
    if client_id is not None:
        log.info("start_download: добавлен из magnet (client_id=%s)",
                 client_id)
        _wait_and_prioritize_async(release.torrent_hash)
    return client_id


# ============================================================
# MAGNET: ОЖИДАНИЕ ФАЙЛОВ В ФОНЕ
# ============================================================

def _wait_and_prioritize_async(torrent_hash: str) -> None:
    """Запускает фоновый поток для magnet-загрузки."""
    t = threading.Thread(
        target=_wait_and_prioritize,
        args=(torrent_hash,),
        daemon=True,
        name=f"prioritize-{torrent_hash[:8]}",
    )
    t.start()


def _wait_and_prioritize(torrent_hash: str,
                         attempts: int = 15,
                         delay: float = 1.0) -> None:
    """
    Для magnet: ждёт появления файлов и ставит приоритет
    на первый аудио. Работает максимум attempts * delay секунд.
    Poller доделает, если и это не успеет.
    """
    for i in range(attempts):
        time.sleep(delay)
        files = get_files(torrent_hash)
        if not files:
            continue

        audio = sorted(
            (f for f in files if is_audio(f.name)),
            key=lambda f: file_order_key(f.name),
        )
        if not audio:
            log.debug("_wait_and_prioritize: файлов %d, аудио нет",
                      len(files))
            return

        first = audio[0]
        log.info(">>> _wait_and_prioritize (magnet): high на id=%s name=%r",
                 first.index, first.name[:60])
        prioritize_for_stream(torrent_hash, first.index)
        return

    log.warning(">>> _wait_and_prioritize (magnet): не дождались за "
                "%.0f сек, poller доделает", attempts * delay)


# ============================================================
# СВЯЗЬ
# ============================================================

def ping() -> bool:
    try:
        return get_backend().ping()
    except Exception:
        log.exception("ping")
        return False


# ============================================================
# ЖИЗНЕННЫЙ ЦИКЛ
# ============================================================

def add_magnet(magnet: str, save_path: Path) -> int | None:
    try:
        enriched = _enrich_magnet(magnet)
        log.info("add_magnet: %d → %d символов",
                 len(magnet), len(enriched))
        return get_backend().add_magnet(enriched, save_path)
    except Exception:
        log.exception("add_magnet(%s...)", magnet[:60])
        return None


def add_torrent_bytes(data: bytes, save_path: Path,
                      auto_priority: bool = True) -> int | None:
    """
    Добавляет торрент из байтов .torrent-файла.
    При auto_priority=True backend сразу выставит приоритеты:
    high на первый аудио, normal на остальные, unwanted на не-аудио.
    """
    try:
        return get_backend().add_torrent_bytes(
            data, save_path, auto_priority=auto_priority)
    except Exception:
        log.exception("add_torrent_bytes")
        return None


def remove(torrent_hash: str, delete_files: bool = True) -> bool:
    try:
        return get_backend().remove(torrent_hash, delete_files=delete_files)
    except Exception:
        log.exception("remove(%s)", torrent_hash[:8])
        return False


# ============================================================
# ЧТЕНИЕ
# ============================================================

def get_torrent(torrent_hash: str) -> TorrentInfo | None:
    try:
        return get_backend().get_torrent(torrent_hash)
    except Exception:
        log.exception("get_torrent(%s)", torrent_hash[:8])
        return None


def list_active() -> list[TorrentInfo]:
    try:
        return get_backend().list_active()
    except Exception:
        log.exception("list_active")
        return []


def get_files(torrent_hash: str) -> list[TorrentFileInfo]:
    try:
        return get_backend().get_files(torrent_hash)
    except Exception:
        log.exception("get_files(%s)", torrent_hash[:8])
        return []


def get_file_path(torrent_hash: str, file_index: int) -> Path | None:
    try:
        return get_backend().get_file_path(torrent_hash, file_index)
    except Exception:
        log.exception("get_file_path(%s, %s)", torrent_hash[:8], file_index)
        return None


# ============================================================
# УПРАВЛЕНИЕ ПРИОРИТЕТАМИ
# ============================================================

def set_file_priorities(torrent_hash: str,
                        high: list[int],
                        normal: list[int],
                        low: list[int] | None = None) -> bool:
    try:
        return get_backend().set_file_priorities(
            torrent_hash, high=high, normal=normal, low=low or [])
    except Exception:
        log.exception("set_file_priorities(%s)", torrent_hash[:8])
        return False


def enable_sequential(torrent_hash: str, enabled: bool = True) -> bool:
    try:
        return get_backend().enable_sequential(torrent_hash, enabled=enabled)
    except Exception:
        log.exception("enable_sequential(%s)", torrent_hash[:8])
        return False


def prioritize_for_stream(torrent_hash: str,
                          current_file_index: int) -> bool:
    """
    Ставит high на текущий файл, normal на остальные аудио и обложки томов,
    low на прочие не-аудио. Sequential включён.

    Обложки (картинки) держим в normal, а не в low — иначе файл обложки
    тома никогда не скачается и книга так и будет с временной обложкой.

    Все три списка передаются явно — чтобы ни один файл не остался
    с приоритетом по умолчанию (Transmission 4.x выставляет low).
    """
    files = get_files(torrent_hash)
    if not files:
        log.warning("prioritize_for_stream: нет файлов для %s",
                    torrent_hash[:8])
        return False

    if current_file_index not in [f.index for f in files]:
        log.warning("prioritize_for_stream: file_index=%s не в торренте",
                    current_file_index)
        return False

    audio_ids = [f.index for f in files if is_audio(f.name)]
    image_ids = [f.index for f in files if is_image(f.name)]
    other_ids = [f.index for f in files
                 if not is_audio(f.name) and not is_image(f.name)]

    if current_file_index not in audio_ids:
        log.warning("prioritize_for_stream: file_index=%s не аудио "
                    "(аудио: %s)", current_file_index, audio_ids[:5])
        return False

    high = [current_file_index]
    normal = [i for i in audio_ids if i != current_file_index]
    normal += image_ids
    low = other_ids

    ok = set_file_priorities(
        torrent_hash,
        high=high,
        normal=normal,
        low=low,
    )

    log.info("prioritize_for_stream: hash=%s high=%s normal=%d low=%d",
             torrent_hash[:8], high, len(normal), len(low))

    if ok:
        enable_sequential(torrent_hash, enabled=True)
    return ok


def audio_file_ids(torrent_hash: str) -> list[int]:
    files = get_files(torrent_hash)
    return [f.index for f in files if is_audio(f.name)]


def find_file_by_index(torrent_hash: str,
                       file_index: int) -> TorrentFileInfo | None:
    for f in get_files(torrent_hash):
        if f.index == file_index:
            return f
    return None