"""
Гарантия единственного экземпляра сервера.

SQLite-файл БД (особенно в режиме VFS "unix-excl", необходимом для
CIFS/SMB-шары) допускает работу ТОЛЬКО одного процесса. Если запускается
второй экземпляр (systemd + nohup, два `./manage.sh start`, ручной
`python main.py`), он падает уже на `PRAGMA journal_mode=WAL` с
"database is locked" — причём не сразу, а после 30-секундного ожидания
(busy_timeout), что сильно затрудняет диагностику.

Модуль использует УЖЕ СУЩЕСТВУЮЩИЙ PID-файл данных
(data_dir/server.pid — тот же, что пишет manage.sh) и до старта проверяет,
что процесс с сохранённым PID ещё жив. Если да — выходим с понятной
ошибкой, не доходя до работы с БД. Отдельный lock-файл не заводим.
"""

import atexit
import logging
import os
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

PID_FILE_NAME = "server.pid"

_pid_path: Path | None = None


class AlreadyRunningError(RuntimeError):
    """Другой экземпляр сервера уже держит БД."""


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с данным PID (без отправки реального сигнала)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # процесс существует, но принадлежит другому пользователю
        return True
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int:
    try:
        return int((path.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def _release() -> None:
    """Удаляет PID-файл, только если он наш (совпадает по PID)."""
    global _pid_path
    if _pid_path is None:
        return
    try:
        if _pid_path.exists() and _read_pid(_pid_path) == os.getpid():
            _pid_path.unlink()
            log.info(">>> singleton: PID-файл удалён %s", _pid_path)
    except OSError:
        pass


def acquire_single_instance() -> None:
    """Захватывает single-instance блокировку через PID-файл.

    Бросает AlreadyRunningError, если её держит живой другой процесс.

    Не использует POSIX/flock-локи: на CIFS/SMB они ненадёжны (та же
    причина, по которой БД открывается через VFS "unix-excl"). Вместо
    этого — атомарное создание файла (O_CREAT|O_EXCL) и проверка живости
    ранее записанного PID через os.kill(pid, 0).
    """
    global _pid_path

    settings.ensure_dirs()
    _pid_path = settings.data_dir / PID_FILE_NAME
    path = _pid_path

    my_pid = os.getpid()

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            old_pid = _read_pid(path)
            if old_pid and old_pid != my_pid and _pid_alive(old_pid):
                raise AlreadyRunningError(
                    f"уже запущен другой экземпляр сервера (PID={old_pid}). "
                    f"Остановите его: ./manage.sh stop, либо "
                    f"'sudo systemctl stop audiobook-server', либо "
                    f"'kill {old_pid}'. PID-файл: {path}"
                )
            # PID мёртв/битый — файл остался от упавшего процесса, снимаем
            log.warning(
                ">>> singleton: найден устаревший PID-файл %s (PID=%s не жив) "
                "— перезаписываю", path, old_pid or "?",
            )
            try:
                path.unlink()
            except OSError:
                pass
            continue
        except OSError as exc:
            # ФС не поддерживает O_EXCL/создание файла (некоторые
            # конфигурации CIFS) — не блокируем запуск, но предупреждаем.
            log.warning(
                ">>> singleton: не удалось создать PID-файл %s (%s) — "
                "защита от второго экземпляра отключена", path, exc,
            )
            _pid_path = None
            return
        else:
            os.write(fd, str(my_pid).encode("ascii"))
            os.close(fd)
            atexit.register(_release)
            log.info(">>> singleton: PID-файл записан %s (PID=%d)", path, my_pid)
            return

    raise AlreadyRunningError(
        f"не удалось получить PID-файл {path} — "
        f"вероятно, параллельно стартует другой экземпляр"
    )
