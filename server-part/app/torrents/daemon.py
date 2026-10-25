"""
Управление процессом торрент-клиента (автозапуск по TORRENT_BACKEND).

Единый механизм: build_launcher() по settings.TORRENT_BACKEND возвращает
один из лаунчеров:

  - TransmissionDaemon      (TORRENT_BACKEND=transmission) — как было:
    готовит settings.json, запускает transmission-daemon с --config-dir,
    ждёт RPC-порт.
  - QBittorrentDaemon       (TORRENT_BACKEND=qbittorrent) — новый:
    ищет qbittorrent.exe / qbittorrent-nox, запускает без лишних аргументов,
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

Про qBittorrent 4.6.3 на Linux (проверено на практике):
  - --profile=<dir> в этой сборке работает не так, как ожидается:
    qBittorrent читает не <dir>/qBittorrent.conf, а что-то другое,
    и в результате игнорирует подготовленный конфиг (генерирует
    временный пароль и заново спрашивает лицензию).
  - --webui-port=<port> без --profile тоже не помогает: конфиг
    читается из другого места, порт берётся из дефолта.
  - РАБОТАЕТ: запуск без --profile и --webui-port. qBittorrent
    читает ~/.config/qBittorrent/qBittorrent.conf (Linux),
    %APPDATA%/qBittorrent/qBittorrent.conf (Windows) и т.п.
    Порт берётся из WebUI\\Port в конфиге, лицензия — из
    [LegalNotice] Accepted=true, пароль — из
    WebUI\\Password_PBKDF2 (формат <salt_b64>:<key_b64>, с двоеточием).
  - Дополнительно нужен WebUI\\Enabled=true (без него WebUI
    стартует нестабильно и генерирует временный пароль).
"""
from __future__ import annotations


import base64
import hashlib
import json
import logging
import os
import platform
import re
import secrets
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
    which_names: list[str] = []

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._we_started = False

    def autostart_enabled(self) -> bool:
        raise NotImplementedError

    def start_timeout_sec(self) -> int:
        return 30

    def is_running(self) -> bool:
        raise NotImplementedError

    def _candidate_binaries(self) -> list[Path]:
        return []

    def _prepare_config(self, binary: Path) -> Path:
        return Path.cwd()

    def _launch_args(self, binary: Path) -> list[str]:
        return [str(binary)]

    def _creation_flags(self, binary: Path) -> int:
        return _CREATION_FLAGS

    def _capture_stderr(self, binary: Path) -> bool:
        return False

    def find_binary(self) -> Path | None:
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

    async def start(self) -> bool:
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

        timeout = self.start_timeout_sec()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.is_running():
                dt = time.time() - t0
                log.info(">>> %s: запущен, порт открыт за %.1f сек",
                         self.display_name, dt)
                return True
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

    async def stop(self) -> None:
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
    display_name = "Transmission daemon"
    which_names = ["transmission-daemon"]

    def autostart_enabled(self) -> bool:
        return settings.TRANSMISSION_AUTOSTART

    def start_timeout_sec(self) -> int:
        return settings.TRANSMISSION_START_TIMEOUT_SEC

    def is_running(self) -> bool:
        return _port_open(settings.TRANSMISSION_HOST, settings.TRANSMISSION_PORT)

    def _default_config_dir(self) -> Path:
        if settings.TRANSMISSION_CONFIG_DIR:
            return Path(settings.TRANSMISSION_CONFIG_DIR)
        if _SYSTEM == "Windows":
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            return Path(base) / "Transmission"
        if _SYSTEM == "Darwin":
            return Path.home() / "Library" / "Application Support" / "Transmission"
        return Path.home() / ".config" / "transmission-daemon"

    def _candidate_binaries(self) -> list[Path]:
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
        return [
            Path(settings.TRANSMISSION_BIN) if settings.TRANSMISSION_BIN else None,
            Path("/usr/bin/transmission-daemon"),
            Path("/usr/local/bin/transmission-daemon"),
            Path("/usr/sbin/transmission-daemon"),
            Path("/usr/local/sbin/transmission-daemon"),
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

def _qbt_password_hash(password: str) -> str:
    """
    Хэш пароля WebUI qBittorrent 4.6.x.

    Формат (проверено на qBittorrent 4.6.3):
        WebUI\\Password_PBKDF2="@ByteArray(<salt_b64>:<key_b64>)"

    ВАЖНО: соль и ключ разделены ДВОЕТОЧИЕМ. Оба куска — base64.
      - salt: 16 байт
      - key:  64 байта (PBKDF2-HMAC-SHA512, 100000 итераций)

    Без двоеточия (единый base64 от salt+key) qBittorrent 4.6.3
    значение не парсит и генерирует временный пароль.
    """
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, 100_000,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")
    return f"@ByteArray({salt_b64}:{key_b64})"


def _add_pref_line(text: str, line: str) -> str:
    """Вставляет строку сразу после заголовка [Preferences]."""
    if "[Preferences]" in text:
        return text.replace("[Preferences]", f"[Preferences]\n{line}", 1)
    return f"[Preferences]\n{line}\n" + text


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
        return [
            Path(settings.QBITTORRENT_BIN) if settings.QBITTORRENT_BIN else None,
            Path("/usr/bin/qbittorrent-nox"),
            Path("/usr/local/bin/qbittorrent-nox"),
            Path("/usr/local/sbin/qbittorrent-nox"),
            Path("/snap/bin/qbittorrent-nox"),
            Path("/usr/bin/qbittorrent"),
            Path("/usr/local/bin/qbittorrent"),
            Path("/snap/bin/qbittorrent"),
        ]

    @staticmethod
    def _is_nox(binary: Path) -> bool:
        return "nox" in Path(binary).name.lower()

    def _creation_flags(self, binary: Path) -> int:
        if self._is_nox(binary):
            return super()._creation_flags(binary)
        return 0

    def _capture_stderr(self, binary: Path) -> bool:
        return not self._is_nox(binary)

    def _default_profile_dir(self) -> Path:
        """
        Путь к каталогу, где лежит qBittorrent.conf.

        Должен совпадать с тем, куда qBittorrent-nox смотрит
        по умолчанию (без --profile):
          - Linux:   ~/.config/qBittorrent
          - macOS:   ~/Library/Application Support/qBittorrent
          - Windows: %APPDATA%/qBittorrent
        """
        if settings.TRANSMISSION_CONFIG_DIR:
            # обратная совместимость: кто-то мог задать единый путь
            return Path(settings.TRANSMISSION_CONFIG_DIR)
        if _SYSTEM == "Windows":
            base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
            return Path(base) / "qBittorrent"
        if _SYSTEM == "Darwin":
            return Path.home() / "Library" / "Application Support" / "qBittorrent"
        return Path.home() / ".config" / "qBittorrent"

    def _prepare_config(self, binary: Path) -> Path:
        """
        Готовит qBittorrent.conf в ДЕФОЛТНОМ для qBittorrent месте.

        Ключевые моменты (проверено на 4.6.3):
          - [LegalNotice] Accepted=true — иначе интерактивный запрос 'y'.
          - WebUI\\Enabled=true — без него WebUI стартует нестабильно.
          - WebUI\\LocalHostAuth=false — иначе API-логин не выдаёт cookie.
          - WebUI\\Password_PBKDF2="@ByteArray(salt:key)" — С ДВОЕТОЧИЕМ.
            Без двоеточия qBittorrent генерирует временный пароль.
          - WebUI\\Port=<port> — берётся из настроек, применяется без
            --webui-port.
          - Downloads\\SavePath — куда складывать торренты.
        """
        profile_dir = self._default_profile_dir()
        log.info(">>> %s: проверяем профиль %s", self.display_name, profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)

        conf = profile_dir / "qBittorrent.conf"
        username = settings.QBITTORRENT_USER or "admin"
        password = settings.QBITTORRENT_PASSWORD or "adminadmin"
        pw_hash = _qbt_password_hash(password)
        port = settings.QBITTORRENT_PORT
        save_path = str(settings.downloads_dir)

        # ---- файла нет: создаём с нуля ----
        if not conf.exists():
            log.info(">>> %s: создаём %s", self.display_name, conf)
            conf.write_text(
                "[LegalNotice]\n"
                "Accepted=true\n"
                "\n"
                "[Preferences]\n"
                f"Downloads\\SavePath={save_path}\n"
                "WebUI\\Enabled=true\n"
                "WebUI\\LocalHostAuth=false\n"
                f'WebUI\\Password_PBKDF2="{pw_hash}"\n'
                f"WebUI\\Port={port}\n"
                f"WebUI\\Username={username}\n",
                encoding="utf-8",
            )
            self._profile_dir = profile_dir
            return profile_dir

        # ---- файл есть: правим точечно ----
        text = conf.read_text(encoding="utf-8")
        original = text

        # 1. LegalNotice: Accepted=true должен быть ДО [Preferences]
        prefs_split = text.split("[Preferences]", 1)
        head = prefs_split[0] if len(prefs_split) == 2 else ""
        if "Accepted=true" not in head:
            log.info(">>> %s: добавляем [LegalNotice] Accepted=true",
                     self.display_name)
            if "[LegalNotice]" in text:
                text = text.replace(
                    "[LegalNotice]", "[LegalNotice]\nAccepted=true", 1,
                )
            elif "[Preferences]" in text:
                text = text.replace(
                    "[Preferences]",
                    "[LegalNotice]\nAccepted=true\n\n[Preferences]", 1,
                )
            else:
                text = "[LegalNotice]\nAccepted=true\n\n" + text

        # 2. WebUI\Enabled=true
        if "WebUI\\Enabled=" not in text:
            log.info(">>> %s: добавляем WebUI\\Enabled=true",
                     self.display_name)
            text = _add_pref_line(text, "WebUI\\Enabled=true")
        elif "WebUI\\Enabled=false" in text:
            log.info(">>> %s: исправляем WebUI\\Enabled=false → true",
                     self.display_name)
            text = text.replace("WebUI\\Enabled=false", "WebUI\\Enabled=true")

        # 3. LocalHostAuth=false (true ломает API: нет cookie SID)
        if "WebUI\\LocalHostAuth=true" in text:
            log.info(">>> %s: исправляем LocalHostAuth=true → false",
                     self.display_name)
            text = text.replace(
                "WebUI\\LocalHostAuth=true", "WebUI\\LocalHostAuth=false",
            )
        elif "WebUI\\LocalHostAuth=" not in text:
            log.info(">>> %s: добавляем WebUI\\LocalHostAuth=false",
                     self.display_name)
            text = _add_pref_line(text, "WebUI\\LocalHostAuth=false")

        # 4. Password_PBKDF2 (если нет)
        if "WebUI\\Password_PBKDF2=" not in text:
            log.info(">>> %s: задаём пароль WebUI (PBKDF2)",
                     self.display_name)
            text = _add_pref_line(
                text, f'WebUI\\Password_PBKDF2="{pw_hash}"',
            )

        # 5. Username
        if "WebUI\\Username=" not in text:
            log.info(">>> %s: задаём логин WebUI = %r",
                     self.display_name, username)
            text = _add_pref_line(text, f"WebUI\\Username={username}")

        # 6. Port (перезаписываем актуальным значением из settings).
        #    repl передаём через lambda, чтобы re.sub не парсил
        #    обратные слэши в "WebUI\\Port=..." как escape-последовательности
        #    (иначе: re.error: bad escape \P).
        if "WebUI\\Port=" in text:
            new_text = re.sub(
                r"WebUI\\Port=\d+",
                lambda _m: f"WebUI\\Port={port}",
                text,
            )
            if new_text != text:
                log.info(">>> %s: обновляем WebUI\\Port → %d",
                         self.display_name, port)
                text = new_text
        else:
            log.info(">>> %s: добавляем WebUI\\Port=%d",
                     self.display_name, port)
            text = _add_pref_line(text, f"WebUI\\Port={port}")

        # 7. Downloads\SavePath
        if "Downloads\\SavePath=" not in text:
            log.info(">>> %s: добавляем Downloads\\SavePath=%s",
                     self.display_name, save_path)
            text = _add_pref_line(text, f"Downloads\\SavePath={save_path}")

        if text != original:
            conf.write_text(text, encoding="utf-8")
            log.info(">>> %s: qBittorrent.conf обновлён", self.display_name)
        else:
            log.info(">>> %s: qBittorrent.conf уже в порядке",
                     self.display_name)

        self._profile_dir = profile_dir
        return profile_dir

    def _launch_args(self, binary: Path) -> list[str]:
        # Для qBittorrent 4.6.3 (проверено эмпирически):
        #   - --profile=<dir>     → qBittorrent читает конфиг не оттуда,
        #                            где мы его создали, и генерирует
        #                            временный пароль + заново спрашивает
        #                            лицензию.
        #   - --webui-port=<port> → аналогично, порт не применяется,
        #                            если конфиг не прочитан.
        # Поэтому запускаем БЕЗ аргументов: qBittorrent читает
        # ~/.config/qBittorrent/qBittorrent.conf (или аналог для ОС),
        # порт берёт из WebUI\Port, лицензию из [LegalNotice].
        # Для GUI-версии тоже без аргументов (как было).
        return [str(binary)]


# ============================================================
# LIBTORRENT (ВСТРОЕННЫЙ, NO-OP)
# ============================================================

class EmbeddedLibtorrentDaemon(TorrentClientDaemon):
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
        return


# ============================================================
# ФАБРИКА ЛАУНЧЕРОВ
# ============================================================

def resolve_mode() -> str:
    kind = settings.TORRENT_BACKEND.strip().lower()
    if kind not in ("qbittorrent", "transmission", "libtorrent"):
        raise ValueError(
            f"Неизвестный TORRENT_BACKEND={settings.TORRENT_BACKEND!r}. "
            f"Допустимые: 'qbittorrent', 'transmission', 'libtorrent'."
        )
    return kind


def build_launcher() -> TorrentClientDaemon:
    kind = resolve_mode()
    if kind == "qbittorrent":
        return QBittorrentDaemon()
    if kind == "transmission":
        return TransmissionDaemon()
    return EmbeddedLibtorrentDaemon()