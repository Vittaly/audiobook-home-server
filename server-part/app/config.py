import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Корень проекта — на два уровня выше этого файла:
# <root>/app/config.py → <root>/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- API ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_BASE_URL: str = "http://127.0.0.1:8000"

    # --- данные ---
    # Путь может быть абсолютным или относительным.
    # Относительный считается от PROJECT_ROOT, а не от CWD.
    DATA_DIR: str = "data"

    # --- логи ---
    LOG_LEVEL: str = "DEBUG"

    # --- скрапер ---
    SCRAPE_INTERVAL_HOURS: int = 6
    SCRAPE_RUN_ON_START: bool = True
    SCRAPE_TOTAL_TIMEOUT_SEC: int = 17200
    SCRAPE_HTTP_TIMEOUT: float = 20.0
    SCRAPE_DELAY_SEC: float = 1.5
    SCRAPE_DETAILS_CONCURRENCY: int = 1
    RUTOR_BASE_URL: str = "https://rutor.info"
    RUTOR_CATEGORY_PATH: str = "/browse/2/0/0/0"

    # --- обложки ---
    COVER_MAX_BYTES: int = 10 * 1024 * 1024
    COVER_RETRY_LIMIT: int = 5

    # --- торренты ---
    LIBTORRENT_PORT: int = 6881
    MIN_FREE_DISK_MB: int = 2048
    TTL_DAYS: int = 14

    # --- mDNS ---
    MDNS_ENABLED: bool = True
    MDNS_SERVICE_TYPE: str = "_audiobook._tcp.local."
    MDNS_HEARTBEAT_SEC: int = 300   # 5 минут
    MDNS_LOCAL_IP: str = ""          # пусто = автоопределение, или явный IP

    # --- торренты ---
    # Единый источник истины: выбирает и API-бэкенд (manager), и лаунчер
    # автозапуска (daemon.build_launcher). Допустимые: qbittorrent | transmission | libtorrent.
    TORRENT_BACKEND: str = "qbittorrent"

    # qBittorrent
    QBITTORRENT_HOST: str = "127.0.0.1"
    QBITTORRENT_PORT: int = 8089
    QBITTORRENT_USER: str = "admin"
    QBITTORRENT_PASSWORD: str = "adminadmin"

    # --- qBittorrent daemon ---
    QBITTORRENT_AUTOSTART: bool = True          # поднимать клиент самим
    QBITTORRENT_BIN: str = ""                   # путь к qbittorrent.exe / qbittorrent-nox (пусто = автопоиск)
    QBITTORRENT_START_TIMEOUT_SEC: int = 30     # сколько ждать WebUI-порт после старта

    # Transmission
    TRANSMISSION_HOST: str = "127.0.0.1"
    TRANSMISSION_PORT: int = 9091
    TRANSMISSION_USER: str = ""
    TRANSMISSION_PASSWORD: str = ""
    TRANSMISSION_POLL_SEC: int = 5

    # --- Transmission daemon ---
    TRANSMISSION_AUTOSTART: bool = True        # поднимать демон самим
    TRANSMISSION_BIN: str = "C:\\Program Files\\Transmission\\transmission-qt.exe"                  # путь к exe (пусто = автопоиск)
    TRANSMISSION_CONFIG_DIR: str = ""           # путь к settings.json (пусто = по ОС)
    TRANSMISSION_START_TIMEOUT_SEC: int = 30    # сколько ждать порт после старта

    # libtorrent (на будущее)
    LIBTORRENT_PORT: int = 6881
    LIBTORRENT_STATE_FILE: str = ""          # путь к resume-файлу сессии

    # --- поиск обложек ---
    COVER_SEARCH_ENABLED: bool = True
    COVER_SEARCH_TIMEOUT: float = 10.0
    COVER_SEARCH_MIN_WIDTH: int = 300

    # --- пути ---

    @property
    def data_dir(self) -> Path:
        p = Path(self.DATA_DIR)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @property
    def db_file(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "app.log"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_file.as_posix()}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.downloads_dir,
                  self.covers_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()