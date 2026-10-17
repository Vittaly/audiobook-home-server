"""
Настройка логирования приложения.

Единый файл логов с ротацией, формат с указанием модуля-источника.
Уровень задаётся через LOG_LEVEL в .env (INFO по умолчанию, DEBUG — для отладки).

Особенности:
  - один файл data/app.log, ротация 10 МБ × 5;
  - формат: время | уровень | имя модуля | сообщение;
  - дублирование в stdout для консоли и docker logs;
  - приглушение шумных библиотек (httpx, uvicorn.access, apscheduler);
  - zeroconf логирует на DEBUG только если LOG_LEVEL=DEBUG.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# эти логгеры всегда на WARNING — они слишком шумные на INFO
QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "uvicorn.access",
    "apscheduler",
    "asyncio",
    "multipart",
    "tzlocal"
)


def setup_logging() -> None:
    """Настраивает root-логгер и все дочерние. Идемпотентно."""
    settings.ensure_dirs()

    level_name = settings.LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)
    debug_mode = level == logging.DEBUG

    # --- root ---
    root = logging.getLogger()
    root.setLevel(level)

    # убираем старые хендлеры (при повторной инициализации)
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # --- файл с ротацией ---
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=10 * 1024 * 1024,   # 10 МБ
        backupCount=5,                # 5 архивных файлов
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # --- консоль ---
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    # --- приглушение шумных логгеров ---
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # --- zeroconf: DEBUG только в debug-режиме ---
    zc_level = logging.DEBUG if debug_mode else logging.WARNING
    logging.getLogger("zeroconf").setLevel(zc_level)

    # --- uvicorn.error: наш формат, без своего ---
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("uvicorn").setLevel(level)

    # --- стартовое сообщение ---
    log = logging.getLogger(__name__)
    log.info("Логирование настроено")
    log.info("  файл:        %s", settings.log_file)
    log.info("  уровень:     %s", level_name)
    log.info("  zeroconf:    %s", logging.getLevelName(zc_level))
    log.info("  ротация:     10 МБ × 5 файлов")
    log.info("  шумные:      %s", ", ".join(QUIET_LOGGERS))