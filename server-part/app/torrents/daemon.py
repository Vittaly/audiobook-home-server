"""
Управление процессом торрент-клиента (автозапуск по TORRENT_BACKEND).

Единый механизм: build_launcher() по settings.TORRENT_BACKEND возвращает
один из лаунчеров:

  - TransmissionDaemon      (TORRENT_BACKEND=transmission) — как было:
    готовит settings.json, запускает transmission-daemon с --config-dir,
    ждёт RPC-порт.
  - QBittorrentDaemon       (TORRENT_BACKEND=qbittorrent) — новый:
    ищет qbittorrent.exe / qbittorrent-nox, запускает с --webui-port,
    ждёт WebUI-порт.
  - EmbeddedLibtorrentDaemon (TORRENT_BACKEND=libtorrent) — no-op:
    процесс не нужен, "запуск" = проверка доступности модуля libtorrent.

Общая логика (TorrentClientDaemon):
  1. Если autostart выключен — просто проверяем, что клиент уже доступен.
  2. Если порт уже слушается — не трогаем (внешний клиент).
  3. Ищем бинарь (явный путь, типовые пути ОС, PATH).
  4. Запускаем процесс (опционально подготовив конфиг).
  5. Ждём, пока порт откроется; при таймауте убиваем.
  При shutdown останавливаем клиент только если сами его запустили.
"""

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_SYSTEM = platform.system()  # 'Windows' | 'Linux' | 'Darwin'

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if _SYSTEM == "Windows" else 0


# ============================================================
# ПРОВЕРКА ПОРТА
# ============================================================

def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ============================================================
# РАСШИФРОВКА КОДА ЗАВЕРШЕНИЯ
# ============================================================

def _describe_exit_code(rc: int | None) -> str:
    """Человекочитаемое описание кода завершения процесса (Windows NT)."""
    if rc is None:
        return "процесс ещё работает"
    # для GUI-приложений коды завершения — это NTSTATUS/коды структурных
    # исключений, которые трактуются как 32-битные беззнаковые
    code = rc & 0xFFFFFFFF
    known = {
        0xC0000142: "STATUS_DLL_INIT_FAILED — приложению не удалось "
                    "инициализировать DLL (часто из-за запуска GUI-программы "
                    "с запретом создания окна)",
        0xC0000005: "STATUS_ACCESS_VIOLATION — нарушение доступа к памяти",
        0xC0000135: "STATUS_DLL_NOT_FOUND — не найдена DLL",
        0xC0000139: "STATUS_ENTRYPOINT_NOT_FOUND — не найден экспорт DLL",
        0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
    }
    name = known.get(code)
    if name:
        return f"0x{code:08X} ({name})"
    if code >= 0xC0000000:
        return f"0x{code:08X} (код NTSTATUS/структурного исключения)"
    if rc < 0:
        return f"0x{code:08X} (сигнал/отрицательный код)"
    return f"0x{code:08X} (обычный код завершения {rc})"


# ============================================================
# БАЗОВЫЙ КЛАСС: ОБЩАЯ ЛОГИКА АВТОЗАПУСКА
# ============================================================

class TorrentClientDaemon:
    """Общая логика запуска/остановки процесса torrent-клиента."""

    display_name = "torrent-клиент"
    # имена бинарей для shutil.which (в порядке приоритета)
    which_names: list[str] = []

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._we_started = False

    # ----------------------------------------------------------
    # ПОДКЛАССЫ ОПРЕДЕЛЯЮТ
    # ----------------------------------------------------------

    def autostart_enabled(self) -> bool:
        """Включён ли автозапуск (читается на момент вызова)."""
        raise NotImplementedError

    def start_timeout_sec(self) -> int:
        """Сколько ждать порт после запуска процесса."""
        return 30

    def is_running(self) -> bool:
        """Клиент уже доступен (слушает RPC/WebUI-порт)?"""
        raise NotImplementedError

    def _candidate_binaries(self) -> list[Path]:
        """Возможные пути к бинарю (None-элементы отфильтровываются)."""
        return []

    def _prepare_config(self, binary: Path) -> Path:
        """Hook: подготовить конфиг перед запуском. Возвращает cwd процесса."""
        return Path.cwd()

    def _launch_args(self, binary: Path) -> list[str]:
        """Аргументы запуска процесса."""
        return [str(binary)]

    def _creation_flags(self, binary: Path) -> int:
        """Флаги создания процесса (Windows). Подкласс может уточнить."""
        return _CREATION_FLAGS

    def _capture_stderr(self, binary: Path) -> bool:
        """Собирать ли stderr процесса (PIPE) для диагностики падений."""
        return False

    # ----------------------------------------------------------
    # ПОИСК БИНАРЯ
    # ----------------------------------------------------------

    def find_binary(self) -> Path | None:
        """Ищет бинарь клиента: типовые пути, затем PATH."""
        for path in self._candidate_binaries():
            if path and path.exists() and path.is_file():
                log.info(">>> %s: найден бинарь %s", self.display_name, path)
                return path

        for name in self.which_names:
            which = shutil.which(name)
            if which:
                log.info(">>> %s: найден в PATH %s", self.display_name, which)
                return Path(which)

        log.error(">>> %s: бинарь не найден. "
                  "Укажи путь в .env или установи клиент.", self.display_name)
        return None

    # ----------------------------------------------------------
    # START
    # ----------------------------------------------------------

    async def start(self) -> bool:
        """
        Запускает клиент, если он не работает.
        Возвращает True, если клиент доступен (был или стал).
        """
        if not self.autostart_enabled():
            log.info(">>> %s: autostart выключен, используем внешний",
                     self.display_name)
            return self.is_running()

        if self.is_running():
            log.info(">>> %s: уже работает (внешний, не трогаем)",
                     self.display_name)
            return True

        binary = self.find_binary()
        if binary is None:
            return False

        try:
            cwd = self._prepare_config(binary)
        except Exception:
            log.exception(">>> %s: не удалось подготовить конфиг",
                          self.display_name)
            return False

        log.info(">>> %s: запускаем %s", self.display_name, binary)
        try:
            self._process = subprocess.Popen(
                self._launch_args(binary),
                stdout=subprocess.DEVNULL,
                stderr=(subprocess.PIPE if self._capture_stderr(binary)
                        else subprocess.DEVNULL),
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
                creationflags=self._creation_flags(binary),
            )
            self._we_started = True
        except Exception:
            log.exception(">>> %s: не удалось запустить", self.display_name)
            self._process = None
            return False

        # ждём, пока порт откроется
        timeout = self.start_timeout_sec()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.is_running():
                dt = time.time() - t0
                log.info(">>> %s: запущен, порт открыт за %.1f сек",
                         self.display_name, dt)
                return True
            # процесс упал?
            if self._process.poll() is not None:
                rc = self._process.returncode
                log.error(">>> %s: процесс завершился с кодом %s "
                          "до открытия порта (%s)",
                          self.display_name, rc, _describe_exit_code(rc))
                log.error(">>> %s: stderr процесса:\n%s",
                          self.display_name, self._read_stderr())
                self._process = None
                self._we_started = False
                return False
            time.sleep(0.5)

        log.error(">>> %s: порт не открылся за %d сек",
                  self.display_name, timeout)
        await self._kill()
        return False

    # ----------------------------------------------------------
    # STOP
    # ----------------------------------------------------------

    async def stop(self) -> None:
        """Останавливает клиент, только если мы его запускали."""
        if not self._we_started or self._process is None:
            log.info(">>> %s: наш процесс отсутствует, "
                     "оставляем внешний как есть", self.display_name)
            return

        log.info(">>> %s: останавливаем pid=%d",
                 self.display_name, self._process.pid)
        await self._kill()
        log.info(">>> %s: остановлен", self.display_name)

    async def _kill(self) -> None:
        if self._process is None:
            return

        try:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning(">>> %s: terminate не сработал, kill",
                            self.display_name)
                self._process.kill()
                self._process.wait(timeout=5)
        except Exception:
            log.exception(">>> %s: ошибка при остановке", self.display_name)
        finally:
            self._process = None
            self._we_started = False

    def _read_stderr(self) -> str:
        """Вычитывает stderr уже завершившегося процесса (для диагностики)."""
        if self._process is None:
            return "(нет процесса)"
        try:
            _, err = self._process.communicate(timeout=3)
        except Exception:
            return "(не удалось прочитать stderr)"
        if not err:
            return "(stderr пуст)"
        for enc in ("utf-8", "cp866"):
            try:
                text = err.decode(enc).strip()
            except (UnicodeDecodeError, ValueError):
                continue
            if text:
                return text
        return err.decode("utf-8", errors="replace").strip() or "(stderr пуст)"


# ============================================================
# TRANSMISSION
# ============================================================

def _expected_settings() -> dict:
    """Параметры, которые должны быть в settings.json."""
    return {
        "rpc-enabled": True,
        "rpc-bind-address": "127.0.0.1",
        "rpc-port": settings.TRANSMISSION_PORT,
        "rpc-username": settings.TRANSMISSION_USER or "",
        "rpc-password": settings.TRANSMISSION_PASSWORD or "",
        "rpc-authentication-required": bool(settings.TRANSMISSION_PASSWORD),
        "rpc-whitelist-enabled": False,
        "rpc-host-whitelist-enabled": False,
        "download-dir": str(settings.downloads_dir),
        "incomplete-dir-enabled": False,
        "peer-port": 51413,
        "dht-enabled": True,
        "pex-enabled": True,
        "lpd-enabled": True,
        "utp-enabled": True,
        "start-added-torrents": True,
    }


def ensure_config(config_dir: Path) -> Path:
    """
    Готовит settings.json:
      - если файла нет — создаёт с нужными полями;
      - если файл есть — обновляет ключевые поля
        (rpc-port, download-dir, rpc-username/password),
        остальные не трогает, чтобы не терять пользовательские настройки.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.json"
    expected = _expected_settings()

    if not config_file.exists():
        log.info(">>> Transmission daemon: создаём %s", config_file)
        config_file.write_text(
            json.dumps(expected, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        return config_file

    # файл есть — читаем, обновляем ключевые поля
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        log.exception(">>> Transmission daemon: settings.json не парсится, "
                      "перезаписываем")
        config_file.write_text(
            json.dumps(expected, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        return config_file

    changed = False
    for key in ("rpc-enabled", "rpc-port", "rpc-bind-address",
                "rpc-whitelist-enabled", "rpc-host-whitelist-enabled",
                "download-dir", "rpc-username", "rpc-password",
                "rpc-authentication-required"):
        if data.get(key) != expected.get(key):
            log.info(">>> Transmission daemon: обновляем %s=%r → %r",
                     key, data.get(key), expected.get(key))
            data[key] = expected[key]
            changed = True

    if changed:
        config_file.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(">>> Transmission daemon: settings.json обновлён")

    return config_file


class TransmissionDaemon(TorrentClientDaemon):
    """Управляет процессом transmission-daemon."""

    display_name = "Transmission daemon"
    which_names = ["transmission-daemon"]

    def autostart_enabled(self) -> bool:
        return settings.TRANSMISSION_AUTOSTART

    def start_timeout_sec(self) -> int:
        return settings.TRANSMISSION_START_TIMEOUT_SEC

    def is_running(self) -> bool:
        return _port_open(settings.TRANSMISSION_HOST, settings.TRANSMISSION_PORT)

    def _default_config_dir(self) -> Path:
        """Путь к каталогу конфигурации по ОС."""
        if settings.TRANSMISSION_CONFIG_DIR:
            return Path(settings.TRANSMISSION_CONFIG_DIR)

        if _SYSTEM == "Windows":
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            return Path(base) / "Transmission"
        if _SYSTEM == "Darwin":
            return Path.home() / "Library" / "Application Support" / "Transmission"
        # Linux
        return Path.home() / ".config" / "transmission-daemon"

    def _candidate_binaries(self) -> list[Path]:
        """Возможные пути к transmission-daemon."""
        if _SYSTEM == "Windows":
            return [
                Path(settings.TRANSMISSION_BIN) if settings.TRANSMISSION_BIN else None,
                Path(r"C:\Program Files\Transmission\transmission-daemon.exe"),
                Path(r"C:\Program Files (x86)\Transmission\transmission-daemon.exe"),
                Path(os.environ.get("ProgramFiles", "") + r"\Transmission\transmission-daemon.exe"),
            ]
        if _SYSTEM == "Darwin":
            return [
                Path(settings.TRANSMISSION_BIN) if settings.TRANSMISSION_BIN else None,
                Path("/opt/homebrew/bin/transmission-daemon"),
                Path("/usr/local/bin/transmission-daemon"),
                Path("/Applications/Transmission.app/Contents/MacOS/transmission-daemon"),
            ]
        # Linux и прочие
        return [
            Path(settings.TRANSMISSION_BIN) if settings.TRANSMISSION_BIN else None,
            Path("/usr/bin/transmission-daemon"),
            Path("/usr/local/bin/transmission-daemon"),
            Path("/snap/bin/transmission-daemon"),
        ]

    def _prepare_config(self, binary: Path) -> Path:
        config_dir = self._default_config_dir()
        self._config_dir = config_dir
        ensure_config(config_dir)
        return config_dir

    def _launch_args(self, binary: Path) -> list[str]:
        config_dir = getattr(self, "_config_dir", None) or self._default_config_dir()
        return [str(binary), "--config-dir", str(config_dir)]


# ============================================================
# QBITTORRENT
# ============================================================

class QBittorrentDaemon(TorrentClientDaemon):
    """Управляет процессом qBittorrent (qbittorrent.exe / qbittorrent-nox)."""

    display_name = "qBittorrent"
    which_names = ["qbittorrent-nox", "qbittorrent"]

    def autostart_enabled(self) -> bool:
        return settings.QBITTORRENT_AUTOSTART

    def start_timeout_sec(self) -> int:
        return settings.QBITTORRENT_START_TIMEOUT_SEC

    def is_running(self) -> bool:
        return _port_open(settings.QBITTORRENT_HOST, settings.QBITTORRENT_PORT)

    def _candidate_binaries(self) -> list[Path]:
        """Возможные пути к qbittorrent.exe / qbittorrent-nox."""
        if _SYSTEM == "Windows":
            locals_ = os.environ.get("LOCALAPPDATA") or ""
            pf = os.environ.get("ProgramFiles") or ""
            pfx86 = os.environ.get("ProgramFiles(x86)") or ""
            return [
                Path(settings.QBITTORRENT_BIN) if settings.QBITTORRENT_BIN else None,
                Path(locals_) / "qBittorrent" / "qbittorrent.exe",
                Path(locals_) / "Programs" / "qbittorrent" / "qbittorrent.exe",
                Path(pf) / "qBittorrent" / "qbittorrent.exe",
                Path(pfx86) / "qBittorrent" / "qbittorrent.exe",
                Path(pf) / "qBittorrent" / "qbittorrent-nox.exe",
                Path(pfx86) / "qBittorrent" / "qbittorrent-nox.exe",
            ]
        if _SYSTEM == "Darwin":
            return [
                Path(settings.QBITTORRENT_BIN) if settings.QBITTORRENT_BIN else None,
                Path("/Applications/qBittorrent.app/Contents/MacOS/qBittorrent"),
                Path("/Applications/qbittorrent.app/Contents/MacOS/qbittorrent"),
            ]
        # Linux и прочие
        return [
            Path(settings.QBITTORRENT_BIN) if settings.QBITTORRENT_BIN else None,
            Path("/usr/bin/qbittorrent-nox"),
            Path("/usr/local/bin/qbittorrent-nox"),
            Path("/usr/bin/qbittorrent"),
            Path("/usr/local/bin/qbittorrent"),
            Path("/snap/bin/qbittorrent-nox"),
            Path("/snap/bin/qbittorrent"),
        ]

    @staticmethod
    def _is_nox(binary: Path) -> bool:
        """True, если бинарь — headless qbittorrent-nox."""
        return "nox" in Path(binary).name.lower()

    def _creation_flags(self, binary: Path) -> int:
        # GUI-версии НЕ передаём CREATE_NO_WINDOW — он ломает инициализацию
        # GUI и даёт 0xC0000142 (STATUS_DLL_INIT_FAILED). Флаг оставляем
        # только для консольного qbittorrent-nox.
        if self._is_nox(binary):
            return super()._creation_flags(binary)
        return 0

    def _capture_stderr(self, binary: Path) -> bool:
        # stderr GUI-версии собираем (PIPE) — по нему диагностируем падение.
        return not self._is_nox(binary)

    def _launch_args(self, binary: Path) -> list[str]:
        # Установленная версия qBittorrent 5.2.1 НЕ знает параметров командной
        # строки GUI: --webui-port валит процесс fast-fail'ом 0xC0000409,
        # --minimized / --no-splash-screen дают "неизвестный параметр"
        # (exit code 1). Поэтому GUI-версию запускаем БЕЗ аргументов —
        # все нужные настройки (WebUI-порт и т.п.) уже в её конфиге.
        args = [str(binary)]
        if self._is_nox(binary):
            # headless-версия понимает --webui-port/--no-splash и т.п.
            args += ["--webui-port", str(settings.QBITTORRENT_PORT)]
        return args


# ============================================================
# LIBTORRENT (ВСТРОЕННЫЙ, NO-OP)
# ============================================================

class EmbeddedLibtorrentDaemon(TorrentClientDaemon):
    """Встроенный libtorrent: процесс не нужен, 'запуск' = проверка модуля."""

    display_name = "libtorrent (встроенный)"

    def __init__(self) -> None:
        super().__init__()
        self._lt = None
        try:
            import libtorrent as lt  # type: ignore[import-not-found]
            self._lt = lt
        except Exception:
            self._lt = None

    def autostart_enabled(self) -> bool:
        return True

    def start_timeout_sec(self) -> int:
        return 0

    def is_running(self) -> bool:
        return self._lt is not None

    def _candidate_binaries(self) -> list[Path]:
        return []

    async def start(self) -> bool:
        ok = self.is_running()
        log.info(">>> libtorrent: модуль %s",
                 "доступен" if ok else "не установлен")
        return ok

    async def stop(self) -> None:
        # встроенный клиент останавливать нечего
        return


# ============================================================
# ФАБРИКА ЛАУНЧЕРОВ
# ============================================================

def resolve_mode() -> str:
    """Нормализованный и валидированный TORRENT_BACKEND.

    Единый источник истины о режиме торрент-клиента.
    Используется и здесь (build_launcher), и в manager._build_backend.
    """
    kind = settings.TORRENT_BACKEND.strip().lower()
    if kind not in ("qbittorrent", "transmission", "libtorrent"):
        raise ValueError(
            f"Неизвестный TORRENT_BACKEND={settings.TORRENT_BACKEND!r}. "
            f"Допустимые: 'qbittorrent', 'transmission', 'libtorrent'."
        )
    return kind


def build_launcher() -> TorrentClientDaemon:
    """Возвращает лаунчер автозапуска по settings.TORRENT_BACKEND."""
    kind = resolve_mode()

    if kind == "qbittorrent":
        return QBittorrentDaemon()
    if kind == "transmission":
        return TransmissionDaemon()
    return EmbeddedLibtorrentDaemon()