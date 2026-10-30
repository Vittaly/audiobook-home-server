# OOP-архитектура: классы Книга, Раздача, Глава, Обложка, Серия

## Проблема текущей архитектуры

Код организован процедурно: `ingest.py` (684 строки), `poller.py` (488 строк), `download.py` (384 строки), `book_covers.py`, `series_detect.py` — наборы функций, принимающие `db: Session` и оперирующие модельками извне. Бизнес-логика размазана, а не живёт в объектах.

## Целевая архитектура

Вся бизнес-логика — методы на моделях `Book`, `Release`, `Chapter`, `Cover`, `Series`. Сохранение в БД — метод объекта, а не абстрактная процедура.

```mermaid
flowchart TB
    subgraph Models
        B[Book
        +find_or_create
        +link_release
        +ingest_chapters
        +sync_chapters
        +detect_series
        +needs_online_cover
        +apply_temp_cover
        +start_download
        +fast_download
        +find_series_peers]
        R[Release
        +find_or_create
        +apply_details
        +ingest_torrent
        +has_chapters
        +ordered_chapters
        +actualize
        +apply_priorities
        +start_download
        +add_person
        +set_person
        +add_rubric]
        S[Series
        +find_or_create
        +start_download]
        P[Person
        +find_or_create]
        RU[Rubric
        +find_or_create]
        C[Chapter
        +set_status
        +mark_ready
        +mark_progress
        +mark_error
        +mark_deleted
        +mark_streamed
        +sync_progress
        +start_download]
        CV[Cover
        +apply_meta
        +bump_retry
        +set_temp
        +needs_search
        +copy_from_torrent
        +start_download]
    end

        C[Chapter
        +set_status
        +mark_ready
        +mark_progress
        +mark_error
        +mark_deleted
        +mark_streamed]
        CV[Cover
        +apply_meta
        +bump_retry
        +set_temp
        +needs_search
        +copy_from_torrent
        +search_online]
    end

    subgraph Orchestrators
        P[poller.py
        вызывает Release и Book]
        RT[routes/books.py
        вызывает Book]
    end

    subgraph Parsers
        RP[rutor.py
        парсинг HTML
        +extract_hidewrap]
        DP[description_parser.py
        парсинг hidewrap]
        ST[structure.py
        разбор торрента]
    end

    P --> R
    P --> B
    RT --> B
    B --> C
    B --> CV
    R --> CV
    RP --> R
    DP --> B
    ST --> R
```

## Ключевое решение: загрузка — метод Книги, а не Раздачи

Торрент — общий для всей раздачи (одного magnet/torrent_hash на все книги раздачи). Но загрузка запускается на уровне **книги**: при `Book.start_download()` торрент добавляется в клиент (если ещё не добавлен), затем выставляются приоритеты — **high только на файлы этой книги**, skip на файлы других книг той же раздачи.

Когда пользователь кликает «Скачать» на книге 3 из 15-томной раздачи, качается только книга 3.

**Исключение:** первая глава книги (Фаза 1) — high только на один файл.

## Детальный дизайн классов

### 1. Book — центральная сущность

```python
class Book(Base):
    __tablename__ = "books"
    # поля: book_hash PK, title, author, year, series_id, series_order,
    #       status, cover_*, downloaded_at, last_accessed

    # --- Поиск / создание ---
    @classmethod
    def find_or_create(cls, db: Session, book_hash: str, **fields) -> Book
        """Находит книгу по хэшу или создаёт новую.
        Заменяет ingest._upsert_book_from_group()."""

    def link_release(self, release: Release) -> None:
        """Создаёт M:N связь BookReleaseLink."""

    # --- Главы ---
    def ingest_chapters(self, group: dict, release: Release) -> int:
        """Создаёт/дополняет главы из группы торрента.
        Заменяет ingest._append_chapters()."""

    def sync_chapters(self, files_by_idx: dict) -> bool:
        """Догоняет статусы глав по данным торрент-клиента.
        Заменяет poller._update_chapters()."""

    def recompute_status(self) -> None:
        """УЖЕ СУЩЕСТВУЕТ. Пересчёт по главам."""

    # --- Серия ---
    def detect_series(self, release: Release, group: dict) -> None:
        """Определяет серию: папка тома / заголовок раздачи / hidewrap.
        Заменяет series_detect.detect_series_for_book()."""

    def find_series_peers(self, db: Session) -> list[Book]:
        """Ищет другие книги той же серии.
        Заменяет series_lookup.lookup_series_books()."""

    # --- Загрузка ---
    async def start_download(self, db: Session) -> DownloadResult:
        """Запускает загрузку ВСЕХ глав этой книги:
          1. Проверяет статус, место на диске.
          2. Добавляет торрент раздачи в клиент (если не активен).
          3. Выставляет приоритеты: high на файлы этой книги,
             skip на файлы других книг раздачи.
          4. Если торрент уже активен — только меняет приоритеты.
        Заменяет download.start()."""

    async def fast_download(self, db: Session) -> DownloadResult:
        """Быстрый старт: cover + первая глава.
        Пользователь может начать слушать почти сразу:
          1. self.cover.start_download() — обложка.
          2. self.chapters[0].start_download() — первая глава.
          3. Торрент добавляется/активируется.
          4. Другие книги раздачи — skip.
          5. Остальные главы этой книги — normal.
        После fast_download остальные главы продолжают
        качаться с normal приоритетом."""

    def apply_temp_cover(self) -> None:
        """Устанавливает cover_is_temp = True (нет своей обложки).
        Заменяет download._apply_temp_cover()."""

    # --- Обложка ---
    def needs_online_cover(self) -> bool:
        """Проверяет, нужен ли онлайн-поиск обложки.
        Заменяет poller._book_needs_online_cover()."""

    # --- Cover как property (Value Object) ---
    @property
    def cover(self) -> Cover:
        return Cover(self)
```

### 2. Release — карточка раздачи

```python
class Release(Base):
    __tablename__ = "releases"
    # поля: id PK, torrent_hash, magnet, title, description,
    #       status, cover_*, persons, rubrics

    # --- Поиск / создание ---
    @classmethod
    def find_or_create(cls, db: Session, item: dict) -> Release:
        """Находит или создаёт раздачу.
        Заменяет ingest._upsert_release()."""

    def apply_details(self, update: dict) -> None:
        """Применяет данные детальной страницы.
        Заменяет ingest._apply_release_update()."""

    # --- Торрент ---
    def ingest_torrent(self, db: Session, files: list[dict],
                       torrent_name: str, covers: CoverService) -> int:
        """Создаёт книги и главы из структуры торрента.
        Для каждой группы вызывает Book.find_or_create + Book.link_release +
        Book.ingest_chapters + Book.detect_series.
        Заменяет ingest.ingest_release()."""

    def has_chapters(self) -> bool:
        """Есть ли хотя бы одна глава.
        Заменяет poller._release_has_chapters()."""

    def ordered_chapters(self) -> list[Chapter]:
        """Главы раздачи в целевом порядке: books по ordinal -> chapters.
        Заменяет poller._ordered_chapters()."""

    def actualize(self, db: Session, torrent_hash: str,
                  torrent_manager) -> bool:
        """Двухфазная схема приоритетов:
          Фаза 1: ни одна глава не готова — high на первую главу раздачи.
          Фаза 2: есть готовая глава — качаются все книги (high на первую
                  незагруженную главу, остальные normal).
        Заменяет poller.actualize_release()."""

    def apply_priorities(self, torrent_hash: str, files,
                         high_file_index: int, phase2: bool) -> None:
        """Выставляет приоритеты файлов в торрент-клиенте.
        Заменяет poller._apply_priorities()."""

    def recompute_status(self) -> None:
        """УЖЕ СУЩЕСТВУЕТ. Агрегация статусов книг."""

    # --- Загрузка ---
    async def start_download(self, db: Session) -> list[DownloadResult]:
        """Загружает ВСЕ книги этой раздачи.
        Проходит по self.books и вызывает Book.start_download()
        для каждой книги."""

    # --- Персоны / рубрики ---
    def add_person(self, role: str, name: str) -> None:
        """Заменяет ingest._link_person()."""

    def set_person(self, role: str, name: str | None) -> None:
        """Заменяет ingest._replace_person()."""

    def add_rubric(self, title: str) -> None:
        """Заменяет ingest._link_rubric()."""

    # --- Cover ---
    @property
    def cover(self) -> Cover:
        return Cover(self)
```

### 3. Chapter — методы уже есть, small additions

```python
class Chapter(Base):
    # set_status(), mark_ready(), mark_progress(), mark_error(),
    # mark_deleted(), mark_streamed() — УЖЕ СУЩЕСТВУЮТ.
    # Дополнительно:

    def sync_progress(self, file_info) -> bool:
        """Синхронизирует прогресс с торрент-клиентом.
        Возвращает True если статус изменился.
        Заменяет часть poller._update_chapters()."""

    async def start_download(self, db: Session) -> DownloadResult:
        """Фаза 1: загружает только ЭТУ главу.
        High priority на один файл, остальные книги раздачи - skip.
        Вызывается автоматически при старте загрузки книги
        или напрямую пользователем."""
```

### 4. Cover — Value Object (НОВЫЙ)

```python
class Cover:
    """Value Object: обложка книги или раздачи.
    Не имеет своей таблицы — оперирует полями cover_* владельца."""

    def __init__(self, owner: Book | Release):
        self._owner = owner

    @property
    def is_settled(self) -> bool:
        """Постоянная обложка есть."""
        return self._owner.cover_cache_path is not None

    @property
    def needs_search(self) -> bool:
        """Нужен ли онлайн-поиск."""
        return (self._owner.cover_cache_path is None
                and self._owner.cover_is_temp
                and (self._owner.cover_retry_count or 0) < COVER_RETRY_LIMIT)

    def apply_meta(self, meta: dict, origin: str, **kw) -> None:
        """Записывает метаданные скачанной обложки.
        Заменяет ingest._apply_cover_meta()."""

    def bump_retry(self) -> None:
        """Увеличивает счётчик ретраев.
        Заменяет ingest._bump_cover_retry(), book_covers._bump_retry()."""

    def set_temp(self) -> None:
        """Включает временную обложку."""

    async def start_download(self, title: str, author: str) -> bool:
        """Цепочка поиска: LitRes -> LiveLib -> Google Books -> ...
        Скачивает обложку и записывает метаданные.
        Заменяет cover_search."""

    def copy_from_torrent(self, hint_path: Path,
                          covers: CoverService) -> bool:
        """Копирует файл обложки из торрента в кэш.
        Заменяет poller._try_copy_book_cover()."""
```

### 5. Series — метод групповой загрузки

```python
class Series(Base):
    # поля: id PK, title, title_norm, cover_*
    # books: relationship -> list[Book]

    @classmethod
    def find_or_create(cls, db: Session, title: str,
                       title_norm: str | None = None) -> Series:
        """Находит серию по title_norm или создаёт новую."""

    def start_download(self, db: Session) -> list[DownloadResult]:
        """Загружает ВСЕ книги этой серии.
        Проходит по self.books, для каждой книги вызывает
        Book.start_download().
        Возвращает список результатов."""
```

### 6. Person — персоны

```python
class Person(Base):
    # поля: id PK, name, name_norm
    # releases: relationship -> list[ReleasePerson]

    @classmethod
    def find_or_create(cls, db: Session, name: str) -> Person:
        """Находит персону по name_norm или создаёт новую.
        Заменяет ingest._ensure_person()."""
```

### 7. Rubric — жанры / рубрики

```python
class Rubric(Base):
    # поля: id PK, kind, title, title_norm

    @classmethod
    def find_or_create(cls, db: Session, kind: str, title: str) -> Rubric:
        """Находит рубрику по kind+title_norm или создаёт новую.
        Заменяет ingest._ensure_rubric()."""
```

## Схема переноса логики (модуль -> класс) — ИТОГ

| Текущий модуль / функция | Финальная локация |
|---|---|
| `ingest._upsert_release()` | `Release.find_or_create()` |
| `ingest._apply_release_update()` | `Release.apply_details()` |
| `ingest.ingest_release()` | `Release.ingest_torrent()` (+ wrapper `ingest_release()` для тестов) |
| `ingest._upsert_book_from_group()` | `Book.find_or_create()` + `Book.link_release()` |
| `ingest._append_chapters()` | `Book.ingest_chapters()` |
| `ingest._apply_cover_meta()` | `Cover.apply_meta()` |
| `ingest._link_person()` | `Release.add_person()` |
| `ingest._replace_person()` | `Release.set_person()` |
| `ingest._link_rubric()` | `Release.add_rubric()` |
| `ingest._ensure_person()` | `Person.find_or_create()` |
| `ingest._ensure_rubric()` | `Rubric.find_or_create()` |
| | |
| `poller._update_chapters()` | `Book.sync_chapters()` (+ `Release.recompute_status()` каскад в поллере) |
| `poller._try_copy_book_cover()` | осталась в поллере (оркестрация, использует `Cover.copy_from_torrent()`/`CoverService`) |
| | |
| `download._apply_temp_cover()` | `Book.apply_temp_cover()` |
| `download.start()` | осталась тонкой обёрткой, делегирует `release.start_download()` + `book.apply_temp_cover()` |
| `download.start_release()` | осталась тонкой обёрткой, делегирует `release.start_download()` |
| | |
| `book_covers._release_author()` | `Release.release_author` (property) — дубль удалён |
| | |
| `series_detect.detect_series_for_book()` | `Book.detect_series()` — модуль УДАЛЁН |
| | |
| НОВОЕ | `Release.start_download()` — **загрузка ВСЕЙ раздачи** (`torrent_manager.start_download(self, Path(self.save_path))`) |
| НОВОЕ | `Release.start_download_file(file_name, priority)` — добавить торрент + приоритет файлу |
| НОВОЕ | `Book.start_download()` — итерация глав -> `chapter.start_download(priority=3/1)` |
| НОВОЕ | `Chapter.start_download(priority)` — делегация в `release.start_download_file(self.rel_path, priority)` |
| НОВОЕ | `Series.start_download()` — итерация по self.books -> Book.start_download() |
| НОВОЕ | `Book.fast_download()` — обложка + первая глава -> остальное в фоне |
| | |
| ОСТАВШЕЕСЯ в poller.py | `actualize_release()`, `_ordered_chapters()`, `_apply_priorities()`, `_prepare_files()`, `_handle_missing()` — цикл поллинга и оркестрация |
| ОСТАВШЕЕСЯ в ingest.py | `_process_item()`, `_ensure_cover_detached()`, `ingest_release()` — оркестрация краулинга и wrapper |
| ОСТАВШЕЕСЯ в download.py | `stop()`, `remove()`, `_reset_release_chapters()`, `_release_torrent_folder()`, `_remove_release_folder()` |

## Оркестраторы остаются тонкими (итоговый вид)

Бизнес-логика — в моделях; job-модули оркестрируют вызовы и держат
торрент-клиент / сессию БД.

```python
# poller.py после рефакторинга (фрагмент _process_torrent)
def _process_torrent(t, covers):
    with session_scope() as db:
        release = db.execute(
            select(Release).where(Release.torrent_hash == t.hash)
        ).scalar_one_or_none()
        if release is None:
            return None, []

        files = manager.get_files(t.hash)
        if not files:
            return None, []

        # если у раздачи ещё нет глав ни у одной книги — создаём книги и главы
        if not any(b.chapters for b in release.books):
            created = release.ingest_torrent(
                db, _prepare_files(files), t.name, covers)
            if not created:
                return None, []
            search_ids = [b.id for b in release.books if b.needs_online_cover]
            actualize_release(db, t.hash, release)   # двухфазные приоритеты
            return "scanned", search_ids

        # догнать статусы глав + двухфазные приоритеты
        changed = actualize_release(db, t.hash, release)
        search_ids = []
        for book in release.books:
            if _try_copy_book_cover(book, covers):   # книжная обложка из торрента
                changed = True
            if book.needs_online_cover:
                search_ids.append(book.id)

        return ("updated" if changed else None), search_ids
```

> `actualize_release()` внутри: `book.sync_chapters(files_by_idx)` по всем книгам,
> при изменении — `release.recompute_status()`, затем `_ordered_chapters()` +
> `_apply_priorities()` (фаза 1/2). Модели не знают про торрент-клиент и пути.

```python
# routes/books.py после рефакторинга (ориентир)
@router.get("/books/{book_id}")
async def get_book(book_id: str, ...):
    book = service.get_book(db, book_id)
    if book is None:
        raise HTTPException(404)
    # синхронный retry обложки при открытии
    if book.needs_online_cover:            # property на Book
        asyncio.create_task(search_book_cover_detached(covers, book.id))
    return _book_to_out(book, api_base)
```

> В итоге свойство `Book.needs_online_cover` заменило поллерный хелпер
> `_book_needs_online_cover` (был сломан: обращался к несуществующему
> `book.release` после перехода на M:N `releases`).

## Этапы реализации

### Этап 1 ✅ — Модели данных (созданы)

### Этап 2 ✅ — Создание Cover + перенос бизнес-логики в методы (ВЫПОЛНЕН)

**Шаг 2.1 ✅**: Создан класс Cover (Value Object)
- файл `server-part/app/db/models/cover.py`
- методы: `apply_meta()`, `set_temp()`, `needs_search`, `copy_from_torrent()`,
  `start_download()`

**Шаг 2.2 ✅**: Методы на Book
- `Book.find_or_create()` + `Book.link_release()` — из `ingest._upsert_book_from_group`
- `Book.ingest_chapters()` — из `ingest._append_chapters`
- `Book.sync_chapters()` — из `poller._update_chapters`
- `Book.detect_series()` — из удалённого `series_detect.detect_series_for_book`
- `Book.needs_online_cover()` — property из `poller._book_needs_online_cover`
- `Book.apply_temp_cover()` — из `download._apply_temp_cover`
- `Book.start_download()` — итерация глав -> `chapter.start_download(priority)`
- `Book.fast_download()` — обложка + первая глава сразу

**Шаг 2.3 ✅**: Методы на Release
- `Release.find_or_create()` — из `ingest._upsert_release`
- `Release.apply_details()` — из `ingest._apply_release_update`
- `Release.ingest_torrent()` — из `ingest.ingest_release`
- `Release.release_author` — property (автор из persons)
- `Release.recompute_status()` — агрегация статусов книг
- `Release.add_person()` / `Release.set_person()` — из `ingest._link_person` / `_replace_person`
- `Release.add_rubric()` — из `ingest._link_rubric`
- `Release.start_download()` — **загрузка ВСЕЙ раздачи** через `torrent_manager.start_download(self, Path(self.save_path))`
- `Release.start_download_file()` — добавление торрента + приоритет конкретному файлу

> Решение: `actualize_release()` и `_apply_priorities()` остались в `poller.py`
> как тонкая оркестрация (см. схему ниже) — бизнес-логика в моделях, цикл
> поллинга в job-модуле.

**Шаг 2.4 ✅**: Методы на Chapter
- `Chapter.set_status()` / `mark_ready()` / `mark_progress()` — каскад в `book.recompute_status()`
- `Chapter.start_download(priority)` — делегирует в `release.start_download_file()`

**Шаг 2.5 ✅**: Методы на Series
- `Series.find_or_create()` — поиск/создание серии
- `Series.start_download()` — итерация по self.books -> Book.start_download()

**Шаг 2.6 ✅**: `extract_hidewrap()` в `description_parser.py` (уже был, перенесено ранее)
- hidewrap-данные прокидываются через `_normalize_details` в group

**Шаг 2.7 ✅**: `Book.detect_series()` — детекция серии из group (hidewrap/название)

**Шаг 2.8 ✅**: `ingest.py` — тонкая обёртка (оркестрация `Release.find_or_create` +
`apply_details` + `ingest_torrent`); `ingest_release()` оставлен как
backward-compat wrapper для тестов

**Шаг 2.9 ✅**: `series_detect.py` — **УДАЛЁН** (0 импортов; логика в `Book.detect_series()`)

**Шаг 2.10 ✅**: `poller.py` — оркестрация:
- `release.ingest_torrent()` вместо `ingest_release()`
- `book.sync_chapters()` + `release.recompute_status()` вместо дублирующего `_update_chapters()`
- `_update_chapters()` и секция «ОБНОВЛЕНИЕ СТАТУСОВ» удалены

**Шаг 2.11 ✅**: `download.py` — `release.start_download()` + `book.apply_temp_cover()`
вместо прямых вызовов менеджера и локального хелпера

**Шаг 2.12 ✅**: Проверка импортов: `Book/Chapter/Release` в poller.py используются
в type hints; `save_path` в success-путях `start()`/`start_release()` теперь
берётся из `release.save_path` (исправлен NameError-регресс)

**Шаг 2.13 ✅**: `book_covers.py` — `release.release_author` вместо
дублирующего `_release_author()`; тесты компилируются

**Шаг 2.14 ✅**: Методы Person (`find_or_create`) и Rubric (`find_or_create`) +
обновлён план рефакторинга (этот документ)

> **Важно**: В ходе интеграции обнаружена системная проблема —
> модули используют `book.release` (единственное число), но в модели
> Book теперь связь M:N `releases` (список). Свойства `book.release` не
> существует → AttributeError в рантайме. Затронутые файлы:
> - `app/lifecycle/cleanup.py:67`
> - `app/api/routes/books.py:143,174`
> - `app/api/routes/covers.py:81`
> - `app/catalog/scan.py:34`
> - `app/catalog/book_covers.py:68`
> - `app/catalog/service.py:113,157,173,208` — включая ORM-запросы `join(Book.release)`
> - `app/catalog/download.py:71,268,307`
>
> **НЕ затрагивает** `chapter.release` — у Chapter связь N:1 с Release
> существует (`app/db/models/chapter.py:73`) и используется в
> `app/api/streaming.py` и в `Chapter.start_download()`.
>
> Необходимо заменить `book.release` на `book.releases[0] if book.releases
> else None` (или добавить property-совместимый accessor на Book) и переписать
> ORM-запросы на `Book.releases` как M:N. Это отдельный проход после
> завершения Этапа 2.

### Этап 3 — Поиск предыдущих книг серии

**Ключевое архитектурное решение:**

Извлечение состава серии (hidewrap → series_entries) — часть контракта каждого источника.

```python
# app/scraper/types.py
@runtime_checkable
class TorrentSource(Protocol):
    ...
    def extract_series_info(self, html: str) -> SeriesInfo | None: ...

@dataclass
class SeriesInfo:
    name: str
    entries: list[SeriesEntry]

@dataclass
class SeriesEntry:
    order: int
    title: str
```

`app/scraper/rutor.py` — класс `RutorSource` реализует `TorrentSource`, включая `extract_series_info()` через `description_parser.extract_hidewrap()`.

**Изменения:**
- `crawler.py` — создаёт `RutorSource()` и вызывает методы источника вместо модульных функций
- `ingest.py` — получает `TorrentSource` инстанс, использует `details.series_info`
- `release.py` — `apply_details()` сохраняет `series_info` в колонку `series_entries` (JSON), создаёт недостающие книги серии
- `Book.find_series_peers()`
- встроить в `Release.ingest_torrent`

### Этап 4 — Быстрый старт fast_download
- `Book.fast_download()` — обложка + первая глава сразу
- внутри вызывает `self.cover.start_download()` и `self.chapters[0].start_download()`
- остальные главы получают normal priority и качаются в фоне
- встроить в `Release.actualize()` (Фаза 1: `book.fast_download()`)

### Этап 5 — Повторный поиск обложки
- Встроить `Book.needs_online_cover()` в `routes/books.py`
- Автоматический retry при открытии карточки книги