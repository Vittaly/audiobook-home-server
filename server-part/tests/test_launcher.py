# -*- coding: utf-8 -*-
"""
Тест механизма автозапуска торрент-клиента (todo автозапуска).

Проверяет:
  1. resolve_mode() — единый источник режима по TORRENT_BACKEND (валидация).
  2. build_launcher() — выбор лаунчера: transmission / qbittorrent / libtorrent.
  3. QBittorrentDaemon: поиск бинаря (QBITTORRENT_BIN), _launch_args
     (--webui-port), запуск через Popen с ожиданием порта, остановка.
  4. TransmissionDaemon (регрессия): ensure_config, _launch_args (--config-dir).
  5. EmbeddedLibtorrentDaemon: no-op start/stop.
  6. Синхронизация manager._build_backend (единый источник режима).
  7. /health отдаёт mode + torrent_client.

Запуск:  venv\\Scripts\\python.exe tests\\test_launcher.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

# Корень проекта — чтобы запускать скрипт и из tests/, и из корня
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ВАЖНО: изолируем DATA_DIR до импорта app.* (engine binds db_url при импорте)
_TMP = tempfile.mkdtemp(prefix="abs_launcher_")
os.environ["DATA_DIR"] = _TMP
os.environ["SCRAPE_RUN_ON_START"] = "false"
os.environ["MDNS_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.server import create_app  # noqa: E402
from app.config import settings  # noqa: E402
from app.torrents import manager  # noqa: E402
import app.torrents.daemon as daemon_mod  # noqa: E402

from app.torrents.daemon import (  # noqa: E402
    EmbeddedLibtorrentDaemon,
    QBittorrentDaemon,
    TransmissionDaemon,
    build_launcher,
    ensure_config,
    resolve_mode,
)


def run(coro):
    return asyncio.run(coro)


def test_resolve_mode():
    with patch.object(settings, "TORRENT_BACKEND", " qBittorrent "):
        assert resolve_mode() == "qbittorrent"
    with patch.object(settings, "TORRENT_BACKEND", "transmission"):
        assert resolve_mode() == "transmission"
    with patch.object(settings, "TORRENT_BACKEND", "libtorrent"):
        assert resolve_mode() == "libtorrent"
    with patch.object(settings, "TORRENT_BACKEND", "weird"):
        try:
            resolve_mode()
        except ValueError:
            pass
        else:
            raise AssertionError("resolve_mode должен бросить ValueError")


def test_build_launcher_choice():
    with patch.object(settings, "TORRENT_BACKEND", "qbittorrent"):
        assert isinstance(build_launcher(), QBittorrentDaemon)
    with patch.object(settings, "TORRENT_BACKEND", "transmission"):
        assert isinstance(build_launcher(), TransmissionDaemon)
    with patch.object(settings, "TORRENT_BACKEND", "libtorrent"):
        assert isinstance(build_launcher(), EmbeddedLibtorrentDaemon)


def test_qbittorrent_binary_search_prefers_bin():
    """QBITTORRENT_BIN — первый кандидат; find_binary находит существующий."""
    with tempfile.TemporaryDirectory(prefix="abs_qb_") as d:
        fake = os.path.join(d, "qbittorrent.exe")
        open(fake, "w").close()

        with patch.object(settings, "QBITTORRENT_BIN", fake):
            launcher = QBittorrentDaemon()
            found = launcher.find_binary()
            assert found is not None
            assert os.path.abspath(str(found)) == os.path.abspath(fake)


def test_qbittorrent_launch_args():
    """GUI-версия 5.2.1: запускаем БЕЗ аргументов. Он не знает CLI-флагов
    (--webui-port валит fast-fail'ом 0xC0000409, --minimized/--no-splash-screen
    дают "неизвестный параметр", exit 1); WebUI-порт берётся из конфига."""
    launcher = QBittorrentDaemon()
    with patch.object(settings, "QBITTORRENT_PORT", 8123):
        args = launcher._launch_args("qbittorrent.exe")
    assert args == ["qbittorrent.exe"]


def test_qbittorrent_nox_launch_args():
    """nox (headless): понимает --webui-port (в отличие от GUI)."""
    launcher = QBittorrentDaemon()
    with patch.object(settings, "QBITTORRENT_PORT", 8123):
        args = launcher._launch_args("qbittorrent-nox.exe")
    assert args == ["qbittorrent-nox.exe", "--webui-port", "8123"]


def test_qbittorrent_gui_no_create_no_window():
    """GUI-бинарь НЕ получает CREATE_NO_WINDOW (причина 0xC0000142)."""
    launcher = QBittorrentDaemon()
    assert launcher._creation_flags("qbittorrent.exe") == 0
    # nox остаётся со стандартным флагом (_CREATION_FLAGS)
    assert (launcher._creation_flags("qbittorrent-nox.exe")
            == daemon_mod._CREATION_FLAGS)
    # stderr собираем только у GUI
    assert launcher._capture_stderr("qbittorrent.exe") is True
    assert launcher._capture_stderr("qbittorrent-nox.exe") is False


def test_describe_exit_code():
    """Расшифровка кода завершения: 3221226505 = 0xC0000409, 0xC0000142 и т.п."""
    from app.torrents.daemon import _describe_exit_code

    text = _describe_exit_code(3221226505)
    assert "0xC0000409" in text
    assert "STATUS_STACK_BUFFER_OVERRUN" in text

    text = _describe_exit_code(-1073741502)  # signed 0xC0000142
    assert "0xC0000142" in text
    assert "STATUS_DLL_INIT_FAILED" in text

    assert _describe_exit_code(0) == "0x00000000 (обычный код завершения 0)"
    assert "процесс ещё работает" in _describe_exit_code(None)


def test_qbittorrent_start_and_stop():
    """Запуск: is_running False→True, Popen (без --webui-port), stop → terminate."""
    launcher = QBittorrentDaemon()

    proc = Mock()
    proc.poll.return_value = None
    proc.wait.return_value = 0

    with patch.object(settings, "QBITTORRENT_PORT", 8123), \
         patch.object(settings, "QBITTORRENT_AUTOSTART", True), \
         patch.object(launcher, "find_binary",
                      return_value="fake-qb.exe"), \
         patch.object(launcher, "is_running", side_effect=[False, True]), \
         patch("app.torrents.daemon.subprocess.Popen", return_value=proc):

        assert run(launcher.start()) is True
        assert launcher._we_started is True
        assert launcher._process is proc

        run(launcher.stop())
        proc.terminate.assert_called_once()
        assert launcher._process is None


def test_qbittorrent_external_already_running():
    """Если клиент уже работает — Popen не вызывается, не трогаем."""
    launcher = QBittorrentDaemon()
    with patch.object(settings, "QBITTORRENT_AUTOSTART", True), \
         patch.object(launcher, "is_running", return_value=True), \
         patch("app.torrents.daemon.subprocess.Popen") as popen:
        assert run(launcher.start()) is True
        popen.assert_not_called()
        assert launcher._we_started is False


def test_qbittorrent_autostart_off():
    """AUTOSTART=false: не запускаем процесс, только проверяем доступность."""
    launcher = QBittorrentDaemon()
    with patch.object(settings, "QBITTORRENT_AUTOSTART", False), \
         patch.object(launcher, "is_running", return_value=True), \
         patch("app.torrents.daemon.subprocess.Popen") as popen:
        assert run(launcher.start()) is True
        popen.assert_not_called()


def test_transmission_ensure_config_and_args():
    """Регрессия: ensure_config создаёт settings.json, args содержат --config-dir."""
    with tempfile.TemporaryDirectory(prefix="abs_tr_") as d:
        with patch.object(settings, "TRANSMISSION_CONFIG_DIR", d):
            config_file = ensure_config(Path(d))
            assert config_file.exists()
            import json
            data = json.loads(config_file.read_text(encoding="utf-8"))
            assert data["rpc-enabled"] is True
            assert data["rpc-port"] == settings.TRANSMISSION_PORT

            launcher = TransmissionDaemon()
            args = launcher._launch_args("fake-transmission-daemon")
            assert args[0] == "fake-transmission-daemon"
            assert args[1:3] == ["--config-dir", d]


def test_transmission_start_regression():
    """Перекладка логики: TransmissionDaemon использует базовый start()."""
    launcher = TransmissionDaemon()
    proc = Mock()
    proc.poll.return_value = None
    proc.wait.return_value = 0

    with patch.object(settings, "TRANSMISSION_AUTOSTART", True), \
         patch.object(settings, "TRANSMISSION_CONFIG_DIR",
                      tempfile.mkdtemp(prefix="abs_tr2_")), \
         patch.object(launcher, "find_binary",
                      return_value="fake-transmission-daemon"), \
         patch.object(launcher, "is_running", side_effect=[False, True]), \
         patch("app.torrents.daemon.subprocess.Popen", return_value=proc):
        assert run(launcher.start()) is True
        assert launcher._we_started is True
        run(launcher.stop())
        proc.terminate.assert_called_once()


def test_embedded_libtorrent_noop():
    """libtorrent: start = проверка модуля, stop ничего не делает."""
    launcher = EmbeddedLibtorrentDaemon()
    # не важен результат (модуль может быть не установлен), главное — не падает
    result = run(launcher.start())
    assert isinstance(result, bool)
    run(launcher.stop())  # no-op
    assert launcher._process is None


def test_manager_backend_sync():
    """manager._build_backend использует тот же режим, что build_launcher."""
    manager.reset_backend()
    with patch.object(settings, "TORRENT_BACKEND", "transmission"):
        backend = manager._build_backend()
        assert type(backend).__name__ == "TransmissionBackend"
    manager.reset_backend()
    with patch.object(settings, "TORRENT_BACKEND", "qbittorrent"):
        backend = manager._build_backend()
        assert type(backend).__name__ == "QBittorrentBackend"
    manager.reset_backend()
    with patch.object(settings, "TORRENT_BACKEND", "libtorrent"):
        backend = manager._build_backend()
        assert type(backend).__name__ == "LibtorrentBackend"
    manager.reset_backend()


def test_lifespan_raises_when_client_unavailable():
    """start() вернул False — сервис должен завершиться, а не работать вхолостую."""
    app = create_app()
    launcher = Mock(spec=QBittorrentDaemon)
    launcher.start = AsyncMock(return_value=False)

    with patch("app.api.server.build_launcher", return_value=launcher):
        try:
            with TestClient(app):
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "lifespan должен бросить RuntimeError при ok=False")


def test_health_reports_mode_and_client():
    """/health отдаёт mode (TORRENT_BACKEND) и статус автозапуска."""
    app = create_app()
    # TestClient без `with` — lifespan не выполняется, state выставляем сами
    client = TestClient(app)

    with patch.object(settings, "TORRENT_BACKEND", "qbittorrent"):
        app.state.torrent_client_ok = True
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "qbittorrent"
        assert body["torrent_client"] == "ready"

        app.state.torrent_client_ok = False
        r = client.get("/health")
        body = r.json()
        assert body["mode"] == "qbittorrent"
        assert body["torrent_client"] == "unavailable"


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\nИтого: {len(fns) - failed}/{len(fns)} прошло")
    raise SystemExit(1 if failed else 0)