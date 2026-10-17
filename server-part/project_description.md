# Audiobook Server — полное описание проекта

Домашний сервер-грабер аудиокниг с торрент-трекера, каталогом для мобильного приложения, стримингом через ExoPlayer, автообнаружением в локальной сети и автоматической очисткой неиспользуемого.

---

## Содержание

1. [Цель проекта](#1-цель-проекта)
2. [Технологический стек](#2-технологический-стек)
3. [Архитектура](#3-архитектура)
4. [Конфигурация](#4-конфигурация)
5. [Логирование](#5-логирование)
6. [Реляционная модель данных](#6-реляционная-модель-данных)
7. [Функциональные модули](#7-функциональные-модули)
8. [API](#8-api)
9. [Поток данных](#9-поток-данных)
10. [Нюансы реализации](#10-нюансы-реализации)
11. [Критерии готовности MVP](#11-критерии-готовности-mvp)
12. [Запрещено](#12-запрещено)
13. [Зависимости](#13-зависимости)
14. [Порядок реализации](#14-порядок-реализации)
15. [Итог](#15-итог)

---

## 1. Цель проекта

Разработать Python-приложение (единая точка входа + набор модулей), которое:

1. Периодически собирает каталог аудиокниг с `rutor.info`.
2. Сразу при парсинге сохраняет обложки в локальный кэш — трекер после этого не нужен.
3. Хранит каталог в реляционной БД SQLite.
4. Управляет загрузкой торрентов через встроенную библиотеку `libtorrent` (без внешнего демона).
5. Отдаёт мобильному приложению REST API для просмотра каталога, запуска загрузки, стриминга и прогрессивного скачивания глав.
6. Поддерживает HTTP Range и все заголовки, критичные для ExoPlayer.
7. Автоматически удаляет книги, не использовавшиеся N дней.
8. Анонсирует себя в локальной сети через mDNS/DNS-SD для автообнаружения клиентом.
9. Пишет всю активность в один лог-файл с указанием модуля-источника.

**Вне scope**: само мобильное приложение, веб-UI, авторизация, биллинг, несколько пользователей.

---

## 2. Технологический стек

| Компонент | Технология |
|---|---|
| Язык | Python 3.12+ |
| Torrent-движок | `libtorrent` (rasterbar), in-process |
| Web-фреймворк | FastAPI + uvicorn |
| БД | SQLite + SQLAlchemy 2.0 |
| Миграции | Alembic |
| Планировщик | APScheduler (AsyncIOScheduler) |
| Парсер | `httpx` + `selectolax` |
| Метаданные аудио | `mutagen` |
| Изображения | `Pillow` |
| mDNS | `zeroconf` |
| Логи | stdlib `logging` + RotatingFileHandler |
| Конфиг | `pydantic-settings` + `.env` |
| Клиент | Android + ExoPlayer (вне scope сервера) |

---

## 3. Архитектура

### 3.1. Точка входа

Один скрипт `main.py`, который в одном asyncio event loop поднимает:

- HTTP API (uvicorn, `log_config=None`, `access_log=False`),
- планировщик (APScheduler),
- фоновый воркер алертов libtorrent,
- mDNS-анонсер.

Все модули импортируются как `app.*`. Никаких subprocess, CLI-обёрток, внешних торрент-демонов.

### 3.2. Структура проекта

```
audiobook-server/
 main.py
 requirements.txt
 .env.example
 .gitignore
 README.md

 app/
    config.py               # настройки и пути
    logging_setup.py
    discovery.py            # mDNS-анонсер
   
    db/
       models.py           # SQLAlchemy-модели
       session.py          # engine, SessionLocal, init_db
   
    scraper/
       rutor.py            # парсинг rutor.info
       normalize.py        # очистка названий
   
    torrents/               # исполнитель: скачал по указанному пути
       session.py          # singleton lt.session
       manager.py          # add/remove/list
       priorities.py       # приоритеты и piece_deadline
       alerts.py           # обработка ses.pop_alerts()
   
    catalog/                # владелец данных
       ingest.py           # scraper  book, person, rubric, series, cover
       scan.py             # torrent  chapter, torrent_file
       covers.py           # класс CoverService
       layout.py           # раскладка папок и путей
       service.py          # read-only для API
   
    lifecycle/
       activity.py         # touch(book_id, action)
       cleanup.py          # TTL, orphan-очистка
   
    api/
       server.py
       schemas.py
       streaming.py        # Range + read_piece + ExoPlayer
       routes/
           books.py
           downloads.py
           stream.py
           covers.py
           persons.py
           rubrics.py
           series.py
           search.py
           health.py
   
    scheduler/
        jobs.py

 data/                       # всё, что создаётся в рантайме
    app.sqlite
    app.log
    app.log.1
    downloads/
       <torrent_name>/
           01.mp3
           cover.jpg
    covers/
       ab/
           abc123...jpg
    tmp/

 tests/
```

### 3.3. Принципы

- **Пакет = домен.** Никаких `utils/`, `helpers/`, `library/`, `storage/`, `covers/`.
- **`scraper/`** — источник «снаружи» (сайт).
- **`torrents/`** — инфраструктура загрузки, тупой исполнитель.
- **`catalog/`** — владелец данных: решает, куда что класть, что видит клиент.
- **`lifecycle/`** — политика хранения.
- **`api/`** — транспорт, не знает про внутренности каталога.
- **`discovery.py`** — анонс в локальной сети.

---

## 4. Конфигурация

`.env`:

```
DATA_DIR=data
API_HOST=0.0.0.0
API_PORT=8000
API_BASE_URL=http://192.168.1.10:8000
LOG_LEVEL=INFO

TTL_DAYS=14
SCRAPE_INTERVAL_HOURS=6
LIBTORRENT_PORT=6881
COVER_MAX_BYTES=10485760
MIN_FREE_DISK_MB=2048

RUTOR_BASE_URL=https://rutor.info
RUTOR_CATEGORY_PATH=/browse/2/0/0/0

MDNS_ENABLED=true
MDNS_SERVICE_TYPE=_audiobook._tcp.local.
```

`app/config.py` — единственное место с путями:

```python
class Settings(BaseSettings):
    DATA_DIR: str = "data"

    @property
    def data_dir(self) -> Path: return Path(self.DATA_DIR).resolve()
    @property
    def db_file(self) -> Path: return self.data_dir / "app.sqlite"
    @property
    def log_file(self) -> Path: return self.data_dir / "app.log"
    @property
    def downloads_dir(self) -> Path: return self.data_dir / "downloads"
    @property
    def covers_dir(self) -> Path: return self.data_dir / "covers"
    @property
    def tmp_dir(self) -> Path: return self.data_dir / "tmp"
    @property
    def db_url(self) -> str: return f"sqlite:///{self.db_file}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.downloads_dir,
                  self.covers_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
```

---

## 5. Логирование

**Единый файл `data/app.log`** с ротацией (10 МБ  5).

Формат:

```
%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s
```

Правила:

- Каждый модуль: `log = logging.getLogger(__name__)` в начале файла.
- Никаких `print()`.
- `httpx`, `httpcore`, `uvicorn.access`, `apscheduler`  WARNING.
- Uvicorn запускается с `log_config=None`, `access_log=False`.
- Access-логи пишутся самими роутерами.

Пример:

```
2026-10-10 14:02:11 | INFO     | app.catalog.ingest            | Найдено 47 книг
2026-10-10 14:02:12 | INFO     | app.catalog.covers            | Обложка книги 42 сохранена
2026-10-10 14:02:14 | INFO     | app.torrents.manager          | Добавлен торрент abc123  data/downloads/Толстой - Война и мир
2026-10-10 14:10:00 | INFO     | app.api.routes.stream         | GET /books/42/stream/7 range=0-
2026-10-10 04:00:00 | WARNING  | app.lifecycle.cleanup         | Удаляем book_id=17, last_accessed=2026-09-01
```

---

## 6. Реляционная модель данных

### 6.1. Таблицы

| Таблица | Роль |
|---|---|
| `book` | Карточка книги |
| `chapter` | Логическая глава |
| `person` | Персона (автор, чтец, переводчик, редактор) |
| `book_person` | M2M книги и персоны с ролью |
| `rubric` | Жанр / тег / рубрика |
| `book_rubric` | M2M книги и рубрики |
| `series` | Цикл книг |
| `source` | Трекер-источник |
| `download` | Сессия загрузки торрента |
| `torrent_file` | Файл внутри торрента |
| `access_log` | История обращений |

### 6.2. `book`

```sql
id               INTEGER PK
title            TEXT
title_norm       TEXT
title_raw        TEXT
description      TEXT
year             INTEGER
language         TEXT
series_id        INTEGER FK  series
series_order     INTEGER
status           TEXT  -- announced|queued|downloading|partial|ready|deleting|deleted|error
priority         INTEGER DEFAULT 0
error_message    TEXT

cover_origin     TEXT  -- 'tracker' | 'torrent' | 'series' | 'upload' | 'online' | NULL
cover_source_url TEXT
cover_cache_path TEXT
cover_mime       TEXT
cover_sha256     TEXT
cover_retry_count INTEGER DEFAULT 0
cover_is_temp     INTEGER DEFAULT 0  -- временная обложка: показываем обложку раздачи
cover_path_hint   TEXT               -- rel-путь картинки тома внутри торрента (для докачки)

added_at         DATETIME
downloaded_at    DATETIME
last_accessed    DATETIME
```

### 6.3. Персоны

```sql
person(id, name, name_norm UNIQUE, created_at)

book_person(
    book_id, person_id, role, ordinal,
    PRIMARY KEY (book_id, person_id, role),
    CHECK (role IN ('author','narrator','translator','editor','illustrator'))
)
```

Один автор в разных написаниях (`Толстой Л.Н.` / `Лев Толстой`) — одна строка в `person` по `name_norm`.

### 6.4. Рубрики

```sql
rubric(id, kind, title, title_norm, UNIQUE(kind, title_norm))
-- kind: 'genre' | 'tag' | 'rubric'

book_rubric(book_id, rubric_id, PRIMARY KEY (book_id, rubric_id))
```

### 6.5. Серия

```sql
series(
    id, title, title_norm UNIQUE,
    cover_source_url, cover_cache_path, cover_mime, cover_sha256
)
```

### 6.6. Загрузка

```sql
download(
    id, book_id FK, source_id FK,
    torrent_hash, magnet, source_url,
    save_path,
    size_bytes,
    state,  -- added|metadata|downloading|finished|paused|error|removed
    progress, downloaded_bytes, uploaded_bytes,
    peers, seeds, speed_bps,
    error_message,
    started_at, finished_at, removed_at
)
```

У одной книги может быть много загрузок — история.

### 6.7. Файл торрента

```sql
torrent_file(
    id, download_id FK,
    file_index, rel_path, abs_path,
    size_bytes, mime_type,
    is_audio, is_image,
    file_offset,
    priority, progress,
    pieces_total, pieces_have, is_complete,
    UNIQUE(download_id, file_index)
)
```

### 6.8. Глава

```sql
chapter(
    id, book_id FK, torrent_file_id FK,
    ordinal, title, duration_sec, is_ready,
    UNIQUE(book_id, ordinal)
)
```

### 6.9. Обложка — атрибут книги, не сущность

Никакой отдельной таблицы `cover`. Поля лежат в `book` и `series`:

- `cover_origin` — откуда пришла (`tracker` / `torrent` / `series` / `upload`).
- `cover_source_url` — URL на трекере. Внутреннее, клиенту никогда не видно.
- `cover_cache_path` — относительный путь в `data/covers/`.
- `cover_sha256` — дедупликация и ETag.
- `cover_mime` — content-type.
- `cover_retry_count` — счётчик неудачных скачиваний.

**Правило**: клиент всегда видит одно поле `cover_url = /books/{id}/cover`. Что за ним — кэш, URL, серия или placeholder — забота сервера.

### 6.10. Целостность

- `PRAGMA foreign_keys = ON` при каждом connect.
- `PRAGMA journal_mode = WAL` — API читает, планировщик пишет, без блокировок.
- FK с явным `ON DELETE`: `CASCADE` для «детей» книги, `SET NULL` для ссылок вроде `chapter.torrent_file_id`.
- Enum — через `CHECK`, значения совпадают со строками Python-Enum.
- Миграции — Alembic, без ручных `ALTER`.

---

## 7. Функциональные модули

### 7.1. `scraper/`

**`rutor.py`**

```python
async def fetch_audiobooks(page: int = 1) -> list[dict]:
    """Возвращает сырые данные. В БД не пишет."""
```

Поля: `torrent_hash`, `magnet`, `title_raw`, `size_bytes`, `description`, `cover_url`, `source_url`. Retry 3 раза, экспоненциальный backoff.

**`normalize.py`** — `clean_title`, `extract_author`, `extract_year`, `extract_narrator`.

### 7.2. `torrents/`

**`session.py`** — singleton `lt.session` с настройками DHT, LSD, UPnP, NAT-PMP, портом из конфига.

**`manager.py`**

```python
def add_magnet(magnet: str, save_path: Path) -> lt.torrent_handle: ...
def remove(hash: str, delete_files: bool = True) -> None: ...
def get_handle(hash: str) -> lt.torrent_handle | None: ...
def list_active() -> list[dict]: ...
def wait_metadata(handle, timeout: int = 60) -> bool: ...
def list_files(handle) -> list[dict]: ...
```

`add_magnet` принимает готовый `save_path` от каталога. Не знает ни про `data/`, ни про структуру папок, ни про санитизацию имён.

**`priorities.py`**

```python
def set_file_priorities(handle, high_ids, normal_ids) -> None:
    """0=ignore, 1=normal, 7=high."""

def setup_full_stream(handle, file_index, audio_ids) -> None:
    """Приоритет текущей главы 7, остальных 1, sequential on.
    Для стрима целой главы клиентом (ExoPlayer)."""

def set_readahead(handle, file_offset, window_bytes, deadline_ms) -> None:
    """set_piece_deadline на окно кусков вперёд.
    Для точечной перемотки внутри главы."""

def reset_readahead(handle, pieces: list[int]) -> None: ...
```

`sequential_download` и `set_piece_deadline` **взаимоисключающие**. Выбор автоматический по Range-заголовку.

**`alerts.py`** — корутина, читает `ses.pop_alerts()` раз в 100 мс:

- `metadata_received_alert`  `catalog.scan.scan_book(book_id)`.
- `piece_finished_alert`  обновление progress (throttled), `chapter.is_ready = True` при завершении файла.
- `read_piece_alert`  отдать данные в стример.
- `torrent_finished_alert`  `download.state='finished'`, `book.status='ready'`.
- `torrent_error_alert`  `book.status='error'`.
- `torrent_removed_alert`  завершение cleanup-шага.

### 7.3. `catalog/`

**`layout.py`** — единственное место, решающее, **куда** класть файлы.

```python
def sanitize_name(name: str) -> str: ...
def book_dir(torrent_name: str) -> Path: ...
def cover_file(sha256: str, ext: str) -> Path: ...
def cover_rel(sha256: str, ext: str) -> str: ...
def abs_cover(rel: str) -> Path: ...
def sha256_bytes(data: bytes) -> str: ...
def atomic_write(target: Path, data: bytes) -> None: ...
def remove_book_dir(torrent_name: str) -> None: ...
def free_space_mb() -> int: ...
```

**`ingest.py`**

```python
async def sync_catalog(covers: CoverService) -> int:
    """scraper  normalize  upsert Book, Person, Rubric, Series.
    Обложка скачивается СРАЗУ при появлении новой книги."""
```

Ключевые шаги:

1. `upsert_book` — идемпотентно по `torrent_hash` / `(title_norm, year)`.
2. Если книга новая и есть `cover_url` со страницы  `cover_origin='tracker'`, **сразу** `await covers.fetch_from_url()`.
3. Если скачалось — `cover_cache_path` заполнен. Если нет — `cover_retry_count++`, повторим фоновой задачей.
4. Синхронизация с серией: обложка копируется либо из книги в серию, либо наоборот.
5. Многотомная раздача (`is_multi`): для каждого тома сохраняется `cover_path_hint` —
   rel-путь картинки внутри торрента (если есть). Если картинки в торренте нет —
   `cover_is_temp = true`: до докачки/онлайн-поиска клиенту временно отдаётся обложка
   раздачи (см. 9.2, 9.5).

**`scan.py`**

```python
def scan_book(book_id: int, covers: CoverService) -> None:
    """После метаданных торрента:
    - список файлов из handle,
    - фильтрация аудио, natural sort, INSERT chapter,
    - заполнение file_offset в torrent_file,
    - если в торренте есть картинка и у книги ещё нет обложки:
        covers.cache_from_torrent(), иначе для многотомной книги
        book.cover_path_hint = rel-путь картинки (докачка после загрузки, см. 9.2).
    """
```

**`covers.py`** — класс `CoverService`:

```python
class CoverService:
    def __init__(self, timeout: float = 10.0): ...
    async def aclose(self) -> None: ...

    async def fetch_from_url(self, owner, url: str) -> bool:
        """Скачать URL, положить в кэш, обновить owner.cover_*."""

    def cache_from_torrent(self, book, image_path: Path) -> bool:
        """Скопировать файл из торрента в кэш. Синхронно при скане."""

    def resolve_url(self, book, api_base: str) -> str:
        return f"{api_base}/books/{book.id}/cover"
```

Создаётся один раз в `create_app()` lifespan, живёт в `app.state.covers`.

**`service.py`** — read-only для API: `list_books`, `get_book`, `get_chapter_full`, `get_chapters`, `list_series`, `list_persons`, `list_rubrics`, `search`, `get_download_status`.

### 7.4. `lifecycle/`

**`activity.py`** — `touch(book_id, action)` обновляет `last_accessed` и пишет `access_log`. Только для `stream` и `download`.

**`cleanup.py`**

```python
async def cleanup_unused() -> int:
    """TTL-удаление: book.status='ready' AND priority=0
    AND last_accessed < now - TTL_DAYS.
    Порядок: status='deleting'  remove_torrent(delete_files=True)
     ждать torrent_removed_alert  rmtree  DELETE chapters
     status='deleted'."""

async def cleanup_orphans() -> int:
    """При старте: папки в downloads/ без активных торрентов  rmtree."""
```

### 7.5. `discovery.py`

Класс `ServiceAnnouncer` на базе `zeroconf`:

- Регистрирует сервис `_audiobook._tcp.local.` с TXT-записями: `version`, `path`, `server_name`.
- Запуск в lifespan FastAPI, остановка при shutdown.
- При отключённом `MDNS_ENABLED` — не запускается.

### 7.6. `api/streaming.py`

Ключевой модуль для ExoPlayer:

```python
async def stream_chapter(chapter_id, range_header) -> Response:
    """Range-стрим с двумя источниками:
    1. chapter.is_ready  FileResponse(abs_path) с Accept-Ranges.
    2. иначе  setup_full_stream + read_piece() через libtorrent.
    Всегда: Content-Length, Accept-Ranges, Content-Type.
    При Range: 206 + Content-Range.
    """
```

Правила:

- Парсит `Range: bytes=start-end`. Если нет — отдаёт целиком (`200`).
- Если `Range: bytes=0-` — включает sequential для текущей главы.
- Если `Range: bytes=N-` (перемотка/возобновление) — устанавливает `set_piece_deadline` на нужные куски.
- Не читает файл с диска, пока `is_ready=False` — только `read_piece()`.
- Не троттлит — отдаёт так быстро, как приходят куски.

### 7.7. `scheduler/jobs.py`

```python
def build_scheduler() -> AsyncIOScheduler:
    """Задачи:
    - sync_catalog: interval = SCRAPE_INTERVAL_HOURS (default 6ч).
    - scan_downloads: interval = 30 сек.
    - cleanup_unused: cron = 04:00 ежедневно.
    - retry_covers: interval = 1 час.
    """
```

Каждая задача обёрнута в декоратор, который логирует исключения и не даёт упасть планировщику.

---

## 8. API

### 8.1. Общие правила

- База: `http://<host>:8000`.
- Формат: JSON, кроме отдачи файлов.
- Даты: ISO 8601 UTC с `Z`.
- Ошибки: `{"detail": "..."}`.
- Access-лог пишется самими роутерами через `log.info("GET /books id=%s", book_id)`.

**HTTP-коды**:

- `200` — успех
- `202` — принято (асинхронный запуск)
- `206` — частичный контент (Range)
- `304` — Not Modified (ETag)
- `400` — неверный запрос
- `404` — не найдено
- `409` — конфликт состояния
- `416` — Range вне диапазона
- `507` — недостаточно места на диске

### 8.2. Служебные

#### `GET /health`

```json
{"status": "ok", "db": "ok", "libtorrent": "ok",
 "active_downloads": 3, "uptime_sec": 86400}
```

### 8.3. Книги

#### `GET /books`

Query: `query`, `status`, `rubric`, `author`, `narrator`, `series_id`, `limit`, `offset`, `sort`, `order`.

```json
{
  "items": [BookOut, ...],
  "total": 1234, "limit": 50, "offset": 0
}
```

**`BookOut`**:

```json
{
  "id": 42,
  "title": "Война и мир",
  "author": "Лев Толстой",
  "narrator": "Иван Иванов",
  "year": 1869,
  "language": "ru",
  "series": {"id": 3, "title": "Русская классика", "order": 1},
  "rubrics": ["классика", "роман"],
  "status": "ready",
  "chapters_count": 12,
  "chapters_ready": 12,
  "total_duration_sec": 43200,
  "size_bytes": 524288000,
  "cover_url": "/books/42/cover",
  "added_at": "2026-10-01T10:00:00Z",
  "last_accessed": "2026-10-09T22:15:00Z"
}
```

#### `GET /books/{id}`

Полная карточка + главы + информация о загрузке.

```json
{
  "id": 42,
  "title": "Война и мир",
  "author": "Лев Толстой",
  "narrators": ["Иван Иванов"],
  "year": 1869,
  "series": {"id": 3, "title": "Русская классика", "order": 1},
  "rubrics": ["классика"],
  "status": "ready",
  "cover_url": "/books/42/cover",
  "chapters": [
    {
      "id": 101, "ordinal": 1, "title": "Том 1. Глава 1",
      "duration_sec": 3600, "size_bytes": 43630592,
      "is_ready": true, "stream_url": "/books/42/stream/101"
    }
  ],
  "download": {"id": 55, "state": "finished", "progress": 1.0}
}
```

#### `GET /books/{id}/cover`

Без сетевых вызовов. `FileResponse` или placeholder.

Пока у книги нет своей обложки и `cover_is_temp = true` — отдаётся обложка раздачи
(карточки) с признаком временной обложки:

```
Content-Type: image/jpeg
Content-Length: 45678
ETag: "abc123..."
Cache-Control: no-cache
X-Cover-Temp: true
```

Клиент видит `X-Cover-Temp: true` и перезапрашивает обложку позже (например, при
следующем открытии/обновлении): как только картинка тома скачается из торрента или
будет найдена онлайн, `cover_is_temp` снимется и обложка станет своей (см. 9.2).

Когда своя обложка есть:

```
Content-Type: image/jpeg
Content-Length: 45678
ETag: "abc123..."
Cache-Control: public, max-age=604800
```

Коды: `200`, `304`, `404`.

### 8.4. Стриминг (для ExoPlayer)

#### `GET /books/{id}/stream/{chapter_id}`

Один эндпоинт, два источника (диск или libtorrent). **Критично для ExoPlayer.**

**Заголовки запроса**:

```
Range: bytes=0-            обязательно
If-None-Match: "<etag>"   опционально
```

**Заголовки ответа (206)**:

```
Accept-Ranges: bytes
Content-Type: audio/mpeg
Content-Length: 1048576
Content-Range: bytes 0-1048575/43630592
ETag: "<sha256>"
```

**Правила**:

- `Range` есть  всегда `206 Partial Content`.
- `Range` нет  `200 OK` с полным `Content-Length`.
- `Content-Range` и `Accept-Ranges` **всегда** присутствуют.
- Если `chapter.is_ready`  `FileResponse` с диска.
- Иначе  `setup_full_stream` + `read_piece()`.
- Побочный эффект: `activity.touch(book_id, 'stream')`.

**Ошибки**: `404`, `409`, `416`.

#### `HEAD /books/{id}/stream/{chapter_id}`

Те же заголовки, без тела.

**Критично для ExoPlayer**: если сервер отдаёт `200` на запрос с `Range`, ExoPlayer может отказаться воспроизводить.

### 8.5. Загрузка книги

#### `POST /books/{id}/download`

Запуск загрузки торрента.

```json
{"book_id": 42, "download_id": 55, "state": "downloading"}
```

Коды: `202`, `404`, `409`, `507`.

Побочные эффекты: `activity.touch(book_id, 'download')`, `manager.add_magnet`, скан, обложка.

#### `GET /books/{id}/status`

Прогресс с разбивкой по главам. Клиент опрашивает раз в 5 сек.

```json
{
  "book_id": 42,
  "status": "partial",
  "state": "downloading",
  "progress": 0.31,
  "downloaded_bytes": 162529280,
  "total_bytes": 524288000,
  "speed_bps": 524288,
  "eta_sec": 690,
  "peers": 12, "seeds": 5,
  "chapters": [
    {"id": 101, "ordinal": 1, "is_ready": true,  "progress": 1.00},
    {"id": 103, "ordinal": 3, "is_ready": false, "progress": 0.42}
  ]
}
```

#### `DELETE /books/{id}/download`

Остановка загрузки. Скачанные главы остаются.

#### `DELETE /books/{id}`

Удаление торрента с файлами. Метаданные остаются, `book.status='deleted'`.

#### `POST /books/{id}/priority`

```json
{"priority": 1}
```

Pinned-книга не удаляется по TTL.

### 8.6. Серии

- `GET /series` — список.
- `GET /series/{id}` — карточка + книги.
- `GET /series/{id}/cover` — обложка серии.

### 8.7. Рубрики

- `GET /rubrics?kind=genre|tag|rubric` — список с `books_count`.

### 8.8. Персоны

- `GET /persons?role=author|narrator&query=...`
- `GET /persons/{id}` — персона + роли + книги.

### 8.9. Поиск

- `GET /search?q=...` — единый поиск по книгам, персонам, сериям.

### 8.10. Заголовки, критичные для ExoPlayer

| Заголовок | Когда | Зачем |
|---|---|---|
| `Accept-Ranges: bytes` | всегда на `/stream` | ExoPlayer понимает поддержку перемотки |
| `Content-Range: bytes X-Y/Z` | при `206` | позиция и полный размер |
| `Content-Length: N` | всегда | без него ExoPlayer не стартует |
| `Content-Type: audio/mpeg` | всегда | выбор кодека |
| `ETag` | `/stream`, `/cover` | кэш, `If-None-Match` |
| `Cache-Control` | `/cover` | кэш обложек |

---

## 9. Поток данных

### 9.1. Наполнение каталога (обложка сразу)

```
scraper.rutor.fetch_audiobooks()  list[dict]
     scraper.normalize
     catalog.ingest.sync_catalog(covers)
         upsert book, person, rubric, series
         book.cover_origin = 'tracker'
         book.cover_source_url = <URL>
         await covers.fetch_from_url(book, url)    СРАЗУ
            ok     cover_cache_path заполнен
            fail   cover_retry_count++, retry позже
         многотомная раздача: для каждого тома
             картинка есть    book.cover_path_hint = rel-путь (докачка после загрузки)
             картинки нет     book.cover_is_temp = true (временно обложка раздачи)
         синхронизация с серией
```

### 9.2. Запуск загрузки книги

Многотомная книга без собственной обложки получает `cover_is_temp = true`: до
появления своей обложки клиенту отдаётся обложка раздачи (см. 9.5).

```
POST /books/{id}/download
     catalog.layout.book_dir(book.title)           каталог решает путь
     torrents.manager.add_magnet(magnet, path)     торрент исполняет
     book.status = 'downloading'
     многотомная книга без обложки: cover_is_temp = true
    (async) metadata_received:
         catalog.scan.scan_book(book_id, covers)
             INSERT chapter, torrent_file (с file_offset)
             natural sort аудио
             image в торренте: book.cover_path_hint = rel-путь картинки
    torrents.poller.poll_downloads(covers)      (фон, каждый цикл)
         cover_path_hint задан и файл скачан
             covers.cache_from_torrent(book, path) -> cover_is_temp = false
         иначе, пока cover_retry_count < COVER_RETRY_LIMIT:
             book_covers.try_search_book_cover()    онлайн-поиск по названию книги
                 найдено    cover_cache_path заполнен, cover_is_temp = false
                 не найдено cover_retry_count++
```

### 9.3. Стриминг (ExoPlayer)

```
GET /books/{id}/stream/{chapter_id}  Range: bytes=0-
     lifecycle.activity.touch(book_id, 'stream')
     chapter.is_ready?
         да   FileResponse + Accept-Ranges + Content-Length
         нет  torrents.priorities.setup_full_stream(handle, file_index, audio_ids)
                   libtorrent: sequential, приоритет 7
                   цикл: wait piece_finished_alert  read_piece  StreamingResponse
                   206 + Content-Range + Accept-Ranges + Content-Length
```

Клиент (ExoPlayer) получает поток, **пишет в локальный файл** и параллельно играет из него. Progressive download.

### 9.4. Возобновление после обрыва

Клиент знает размер локального файла N. Делает `Range: bytes=N-`. Сервер:

- если глава `is_ready` — `FileResponse` с диска, отдаёт с N.
- если качается — `_stream_from_lt` с `start=N`, устанавливает дедлайны на нужные куски.

Клиент дописывает с той же позиции.

### 9.5. Отдача обложки

```
GET /books/{id}/cover
     book.cover_cache_path?
        да   FileResponse + ETag + Cache-Control
        нет  дальше:
     book.cover_is_temp?
        да   обложка раздачи + X-Cover-Temp: true + Cache-Control: no-cache
             (клиент перезапрашивает обложку, пока cover_is_temp не снимется,
             см. 9.2; заголовки и кэширование — секция 8.3)
        нет  placeholder.jpg
```

Без сетевых вызовов.

### 9.6. TTL-очистка

```
cron 04:00  lifecycle.cleanup.cleanup_unused()
     SELECT book WHERE status='ready' AND priority=0
      AND last_accessed < now - TTL_DAYS
     status='deleting'
     ses.remove_torrent(handle, delete_files=True)
     ждать torrent_removed_alert
     rmtree(book_dir); DELETE chapters
     status='deleted'
```

Обложки не удаляются — они в `data/covers/`.

### 9.7. Автообнаружение

```
main.py  ServiceAnnouncer.start()
     zeroconf.register_service(
          _audiobook._tcp.local.,
          TXT: version=1.0, path=/api, server_name=<hostname>)
     клиент (NsdManager на Android) находит  resolve  IP:port
```

---

## 10. Нюансы реализации

### 10.1. libtorrent

- Хендлы не кэшируются между вызовами: `session.find_torrent(hash)`.
- `get_session()` — ленивый singleton.
- Вызовы, которые могут блокировать, — через `asyncio.to_thread()`.
- `alert_mask = all_categories`.
- `ses.pop_alerts()` — раз в 100 мс в отдельной корутине.

### 10.2. Стриминг

- `sequential_download(True)` и `set_piece_deadline()` **несовместимы**. Для стрима целой главы — sequential. Для точечной перемотки — deadline.
- `read_piece()` возвращает данные только если кусок есть. Иначе — алерт с ошибкой.
- При паузе стрима окно дедлайнов сбрасывается.
- Прямое чтение файла с диска — только при `is_ready=True`.

### 10.3. Обложки

- Скачиваются сразу при ingest, пока трекер жив.
- Копируются из торрента синхронно при скане, если своей не было.
- Дедупликация по `cover_sha256`.
- Шардирование `data/covers/<sha[:2]>/<sha>.<ext>`.
- `atomic_write` через `data/tmp/`.
- Retry-задача раз в час для `cover_retry_count < 5`.
- Fallback на серию решается на ingest копированием полей, не по запросу.
- Tracker-обложка не перезаписывается из торрента.

### 10.4. Идемпотентность

- `sync_catalog` — upsert по `torrent_hash` / `(title_norm, year)`.
- `init_db` — безопасен при повторном вызове.
- `cleanup_unused` — повторный запуск не вредит.
- `fetch_and_cache` — проверяет sha256 перед скачиванием.

### 10.5. Файловая система

- libtorrent сохраняет файлы в подпапку с именем торрента.
- `sanitize_name` чистит запрещённые символы, обрезает до 200.
- `chapter.rel_path` — путь относительно `downloads/`.
- `book.cover_cache_path` — путь относительно `covers/`.
- Всё через `pathlib.Path`.

### 10.6. Кроссплатформенность

- Windows, macOS, Linux.
- `libtorrent` через `apt`, `brew` или `conda`.
- Никаких `os.fork`, `signal.SIGUSR1`.

### 10.7. Обработка ошибок

- Все внешние вызовы — в `try/except` с логированием.
- Задачи планировщика обёрнуты декоратором.
- API возвращает осмысленные HTTP-коды.
- Книга в `error` не выпадает из каталога.

### 10.8. mDNS

- Имя сервиса уникально — содержит IP или hostname.
- При ошибке регистрации сервер продолжает работу.
- TXT-записи: `version`, `path`, `server_name` для проверки совместимости.

---

## 11. Критерии готовности MVP

- [ ] `python main.py` поднимает API, планировщик, воркер алертов, mDNS-анонсер.
- [ ] Логи — в `data/app.log` с именами модулей.
- [ ] `GET /health`  200.
- [ ] `sync_catalog` кладёт 10 книг, обложки скачаны сразу. Повтор не создаёт дублей.
- [ ] `POST /books/{id}/download` запускает загрузку, первый аудиофайл качается первым.
- [ ] `GET /books/{id}` возвращает карточку с главами и `cover_url`.
- [ ] `GET /books/{id}/cover` без сетевых вызовов. Placeholder, если нет.
- [ ] `GET /books/{id}/stream/{chapter_id}` с `Range: bytes=0-` отдаёт `206` + `Accept-Ranges` + `Content-Length` + `Content-Range`.
- [ ] ExoPlayer на Android воспроизводит главу без полной загрузки.
- [ ] Перемотка и возобновление через `Range: bytes=N-`.
- [ ] Клиент может сохранять поток в файл и играть из файла параллельно.
- [ ] `cleanup_unused` удаляет книгу с истёкшим TTL, включая файлы.
- [ ] mDNS-анонс виден клиенту в локальной сети.
- [ ] Работает на двух из трёх ОС.

---

## 12. Запрещено

- Внешние торрент-демоны (`transmission-rpc`, `qbittorrent-api`).
- `print()` вместо логгера. Логи в разных файлах.
- Пакеты `library/`, `utils/`, `helpers/`, `storage/`, `covers/` без предметной причины.
- Отдельные CLI-скрипты вместо модулей — только `main.py`.
- Чтение файла с диска при стриминге в обход `read_piece()`, пока `is_ready=False`.
- Кэширование хендлов libtorrent между вызовами.
- In-memory очереди между модулями — только БД и алерты.
- Хранение blob-обложек в БД.
- Отдача внешнего `cover_source_url` клиенту.
- Сетевые вызовы в API-роутах.
- Отдельная сущность «обложка» — это атрибут книги.
- Отвечать `200` на запрос с `Range`.
- Опускать `Content-Length` или `Accept-Ranges` на `/stream`.
- Возвращать `null` в `cover_url`.

---

## 13. Зависимости

```
fastapi
uvicorn[standard]
pydantic-settings
sqlalchemy>=2.0
alembic
apscheduler
httpx
selectolax
mutagen
pillow
zeroconf
python-libtorrent
```

---

## 14. Порядок реализации

1. `app/config.py`, `app/logging_setup.py`, `app/db/models.py`, `app/db/session.py`.
2. `main.py` — каркас, `/health`, пустой планировщик. Проверить единый лог.
3. `app/torrents/session.py`, `manager.py` — smoke: magnet, метаданные, файлы.
4. `app/torrents/priorities.py`, `alerts.py` — sequential, приоритеты, алерты.
5. `app/catalog/layout.py`, `covers.py` (класс `CoverService`).
6. `app/scraper/rutor.py`, `normalize.py`, `app/catalog/ingest.py` — 10 книг + обложки.
7. `app/catalog/service.py`, `app/api/routes/books.py` — `GET /books`.
8. `app/catalog/scan.py`, `app/api/routes/downloads.py` — `POST /download`, `GET /status`.
9. `app/api/routes/covers.py` — `GET /books/{id}/cover`.
10. `app/api/streaming.py`, `routes/stream.py` — Range-стрим для ExoPlayer.
11. `app/api/routes/{persons,rubrics,series,search}.py`.
12. `app/lifecycle/activity.py`, `cleanup.py` — TTL.
13. `app/discovery.py` — mDNS-анонсер.
14. `app/scheduler/jobs.py` — всё вместе.
15. Тесты: `sync_catalog`, `scan_book`, `cleanup_unused`, стриминг с Range.

---

## 15. Итог

Сервер — единый Python-процесс с in-process libtorrent, реляционной БД SQLite, REST API, совместимым с ExoPlayer, и mDNS-анонсом в локальной сети. Клиент на Android находит сервер автоматически, получает каталог, запускает загрузку и стримит главы с progressive download: сервер отдаёт поток максимально быстро с диска (если готово) или из libtorrent (если качается), клиент пишет файл и играет из него. Обложки скачиваются сразу при парсинге и не зависят от трекера. TTL-очистка удаляет неиспользуемое, не трогая pinned-книги. Вся активность — в одном логе с именами модулей.