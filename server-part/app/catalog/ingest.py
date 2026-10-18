"""
Наполнение каталога.

Принимает уже нормализованные данные:
  - из crawler.walk — items с готовыми полями
  - из rutor.fetch_torrent_details — готовый update

Никакой нормализации данных не делает. Только пишет в БД.

Модель:
  Release = карточка раздачи (title, description, year, language,
            persons, rubrics, cover, status).
  Book    = книга (том) внутри раздачи (1:N). Своя обложка, свой порядок,
            серия, статус; главы нумеруются локально внутри книги.

Правило: title и author карточки устанавливаются один раз в _upsert_release
из title_raw (заголовок раздачи). Поля формы на детальной странице
ненадёжны (заливщик может путать «Название» и «Автор»), поэтому
_apply_release_update их не перезаписывает.

Обложка карточки: сначала URL с трекера, потом поиск в интернете
(app.catalog.cover_search). Если и там ничего — карточка остаётся
без обложки, retry_count++.
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.covers import CoverService
from app.catalog.layout import sanitize_name
from app.catalog.series_detect import detect_series_for_book
from app.config import settings
from app.db.models import (
    Book, BookStatus, Chapter, ChapterStatus, Person, Release,
    ReleasePerson, ReleaseRubric, Rubric,
)
from app.db.session import SessionLocal
from app.scraper import crawler, rutor
from app.scraper.state import CrawlStateStore
from app.torrents import structure

log = logging.getLogger(__name__)


@contextmanager
def _sync_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# СИНХРОНИЗАЦИЯ КАТАЛОГА
# ============================================================

async def sync_catalog(covers: CoverService) -> int:
    log.info(">>> sync_catalog: старт")
    timeout = settings.SCRAPE_TOTAL_TIMEOUT_SEC
    try:
        result = await asyncio.wait_for(
            _sync_catalog_inner(covers),
            timeout=timeout,
        )
        log.info(">>> sync_catalog: завершено, обработано %d", result)
        return result
    except asyncio.TimeoutError:
        log.warning(">>> sync_catalog: прерван по таймауту %d сек", timeout)
        return 0
    except asyncio.CancelledError:
        # Иногда wait_for пробрасывает CancelledError внутренней задачи вместо
        # TimeoutError (особенно при остановке планировщика). Это ожидаемое
        # событие, а не ошибка — логируем и завершаемся.
        log.info(">>> sync_catalog: отменён")
        return 0


async def _sync_catalog_inner(covers: CoverService) -> int:
    sem = asyncio.Semaphore(max(1, settings.SCRAPE_DETAILS_CONCURRENCY))
    total = 0

    with _sync_session() as db:
        store = CrawlStateStore(db)
        state = store.load("rutor.info", rutor.BASE, "audiobooks")
        log.info(">>> sync_catalog: state = {oldest_seen=%s, resume_page=%s}",
                 state.oldest_seen_at, state.resume_page)

    def on_progress(page: int, st):
        with _sync_session() as db:
            s = db.merge(st)
            CrawlStateStore(db).save_progress(s)

    page_num = 0
    async for page in crawler.walk(
        query="mp3",
        state=state,
        on_progress=on_progress,
    ):
        log.info(">>> sync_catalog: страница %d (%d записей)",
                 page_num, len(page))

        for i, item in enumerate(page):
            try:
                total += await _process_item(item, covers, sem)
                if (i + 1) % 10 == 0:
                    log.info(">>> sync_catalog: %d/%d, всего %d",
                             i + 1, len(page), total)
            except Exception:
                log.exception(">>> sync_catalog: ошибка %s",
                              item.get("torrent_hash"))

        page_num += 1

    with _sync_session() as db:
        CrawlStateStore(db).finish(db.merge(state))

    log.info(">>> sync_catalog: обход завершён, всего %d", total)
    return total


async def _process_item(item: dict, covers: CoverService,
                        sem: asyncio.Semaphore) -> int:
    """
    Обрабатывает одну запись поисковой выдачи (upsert карточки,
    дозаполнение деталей и обложки).

    ВАЖНО про блокировки SQLite (WAL): карточку upsert'им и КОММИТИМ
    отдельной короткой транзакцией, а сетевые запросы (детальная страница,
    обложка) выполняем ПОСЛЕ коммита, когда write-лок уже освобождён.

    Если держать write-лок (взятый на db.flush() в _upsert_release) через
    await'ы сети, другой пишущий — поллер торрентов — ждёт до
    busy_timeout=30с и падает с "database is locked": статусы глав не
    становятся ready, стриминг отдаёт 409, плеер клиента висит в ожидании.
    """
    with _sync_session() as db:
        release = _upsert_release(db, item)
        if release is None:
            return 0
        release_id = release.id
        need_details = (release.description is None
                        and bool(item.get("source_url")))
        need_cover = release.cover_cache_path is None
    # write-лок освобождён: _sync_session уже закоммитил upsert

    # --- детальная страница (сеть без открытого write-лока) ---
    if need_details:
        raw = await _fetch_details(sem, item["source_url"])
        if raw:
            with _sync_session() as db:
                rel = db.get(Release, release_id)
                if rel is not None:
                    _apply_release_update(db, rel, raw)

    # --- обложка карточки (сеть без открытого write-лока) ---
    if need_cover:
        with _sync_session() as db:
            rel = db.get(Release, release_id)
            if rel is not None:
                await _ensure_cover(sem, rel, covers)

    return 1


# ============================================================
# UPSERT КАРТОЧКИ РАЗДАЧИ
# ============================================================

def _parse_tracker_date(value: str | None) -> datetime:
    """Дата создания раздачи на трекере ('YYYY-MM-DD') -> datetime (UTC).

    Если дата отсутствует или некорректна — текущее время (UTC), чтобы
    NOT NULL-поле released_at всегда было заполнено.
    """
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            log.warning(">>> ingest: некорректная дата трекера: %r", value)
    return datetime.now(timezone.utc)


def _upsert_release(db: Session, item: dict) -> Release | None:
    """item нормализован rutor.fetch_search_page: title, author, year готовы."""
    torrent_hash = item["torrent_hash"]

    release = db.execute(
        select(Release).where(Release.torrent_hash == torrent_hash)
    ).scalar_one_or_none()

    if release is not None:
        release.last_seen_at = datetime.now(timezone.utc)
        # Легаси-запись без даты трекера: заполняем один раз. Уже
        # установленную дату не трогаем, чтобы не сбрасывать её при обходе.
        if release.released_at is None:
            release.released_at = _parse_tracker_date(item.get("added_at"))
        return release

    # Раздача без torrent_hash непригодна для скачивания — пропускаем,
    # чтобы не ронять транзакцию на NOT NULL constraint.
    if not torrent_hash:
        title_raw = item.get("title_raw") or item.get("title") or "?"
        log.warning(">>> sync_catalog: пропуск раздачи без torrent_hash: %s",
                    title_raw)
        return None

    title = item["title"]
    title_norm = _norm(title)
    year = item.get("year")

    release = db.execute(
        select(Release).where(
            Release.title_norm == title_norm,
            Release.year == year,
        )
    ).scalar_one_or_none()

    if release is None:
        release = Release(
            title=title,
            title_norm=title_norm,
            title_raw=item.get("title_raw"),
            year=year,
            status=BookStatus.ANNOUNCED.value,
            # обязательные NOT NULL поля задаём сразу при создании —
            # первый flush не должен ловить IntegrityError.
            torrent_hash=torrent_hash,
            magnet=item["magnet"],
            save_path=str(settings.downloads_dir / sanitize_name(title)),
            content_type=item["content_type"],
            released_at=_parse_tracker_date(item.get("added_at")),
        )
        db.add(release)
        db.flush()

        if item.get("author"):
            _link_person(db, release, "author", item["author"])
        if item.get("narrator"):
            _link_person(db, release, "narrator", item["narrator"])

    release.source_id = _ensure_source(db).id
    release.torrent_hash = torrent_hash
    release.magnet = item["magnet"]
    release.source_url = item.get("source_url")
    release.save_path = str(settings.downloads_dir / sanitize_name(title))
    release.title_raw = item.get("title_raw")
    release.size_bytes = item.get("size_bytes")
    release.content_type = item["content_type"]
    release.books_hint = item["books_hint"]
    release.last_seen_at = datetime.now(timezone.utc)
    db.flush()

    return release


# ============================================================
# ПРИМЕНЕНИЕ UPDATE ИЗ ДЕТАЛЬНОЙ
# ============================================================

def _apply_release_update(db: Session, release: Release, update: dict) -> None:
    """
    Применяет поля с детальной страницы.

    НЕ трогает title и author: заголовок раздачи надёжнее полей формы.
    Поля формы используем только для того, чего нет в заголовке.
    """
    if update.get("description"):
        release.description = update["description"]

    # year уточняем только если в поисковой выдаче его не было
    if update.get("year") and release.year is None:
        release.year = update["year"]

    # чтец — отдельное поле, ему доверяем
    if update.get("narrator"):
        _replace_person(db, release, "narrator", update["narrator"])

    # рубрики
    for tag in update.get("rubrics") or []:
        _link_rubric(db, release, tag)

    # обложка-URL: только если ещё не установлена
    if not release.cover_source_url and update.get("cover_url"):
        release.cover_source_url = update["cover_url"]
        release.cover_origin = "tracker"

    # content_type
    if update.get("content_type"):
        release.content_type = update["content_type"]
        if update.get("books_hint"):
            release.books_hint = update["books_hint"]


# ============================================================
# ОБЛОЖКИ: ТРЕКЕР → ИНТЕРНЕТ
# ============================================================

async def _ensure_cover(sem: asyncio.Semaphore, release: Release,
                        covers: CoverService) -> None:
    """
    Пытается получить обложку карточки раздачи:
      1. Скачать по cover_source_url (если он есть).
      2. Искать в интернете (cover_search) — если настройка включена.
      3. Оставить без обложки, cover_retry_count++.
    """
    if release.cover_cache_path is not None:
        return

    # --- 1. трекер ---
    if release.cover_source_url:
        async with sem:
            await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
            if await covers.fetch_from_url(release, release.cover_source_url):
                return
        release.cover_retry_count += 1

    # --- 2. интернет ---
    if not settings.COVER_SEARCH_ENABLED:
        return

    from app.catalog.cover_search import search_cover

    title = _clean_release_title(release.title)
    author = _release_author(release)

    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        log.info("release=%s: ищем обложку в интернете (%s — %s)",
                 release.id, author, title)
        result = await search_cover(title, author)

    if result is None:
        log.warning("release=%s: обложка не найдена нигде", release.id)
        return

    # скачиваем найденную
    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        if await covers.fetch_from_url(release, result.url):
            release.cover_origin = "online"
            log.info("release=%s: обложка из %s", release.id, result.source)
            return

    release.cover_retry_count += 1


def _clean_release_title(title: str) -> str:
    """'Автор - Название' → 'Название'. Для поиска в интернете."""
    if " - " in title:
        return title.split(" - ", 1)[1].strip()
    return title.strip()


def _release_author(release: Release) -> str:
    """Имя автора карточки. Пустая строка, если автора нет."""
    for rp in release.persons:
        if rp.role == "author":
            return rp.person.name
    return ""


# ============================================================
# СЕТЕВЫЕ ХЕЛПЕРЫ
# ============================================================

async def _fetch_details(sem: asyncio.Semaphore, url: str) -> dict | None:
    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        return await rutor.fetch_torrent_details(
            url, http_timeout=settings.SCRAPE_HTTP_TIMEOUT)


# ============================================================
# НАПОЛНЕНИЕ КНИГ ИЗ ТОРРЕНТА
# ============================================================

def ingest_release(release_id: int, files: list[dict], torrent_name: str,
                   covers: CoverService) -> int:
    """
    Создаёт книги внутри раздачи по фактической структуре торрента.

    Для каждой группы-книги:
      - создаёт Book (ordinal, folder, title, своё cover_*);
      - создаёт главы с локальным ordinal внутри книги;
      - копирует обложку тома из торрента (если файл уже на диске),
        иначе сохраняет cover_path_hint для последующей докачки;
      - определяет серию (глобально по каталогу).

    Книга без своей обложки получает cover_is_temp=True: пока своя обложка
    не получена (файл из торрента докачан или найден онлайн), клиент видит
    обложку карточки-раздачи — осмысленную картинку, а не заглушку.

    Возвращает количество созданных глав.
    """
    with _sync_session() as db:
        release = db.get(Release, release_id)
        if release is None:
            return 0

        groups = structure.split_torrent_into_books(files, torrent_name)
        if not groups:
            return 0

        is_multi = len(groups) > 1

        if is_multi and release.content_type == "single":
            release.content_type = "series"
            release.books_hint = len(groups)

        release.folder_map = json.dumps([
            {"folder": g["folder"], "title": g["title"], "order": i + 1}
            for i, g in enumerate(groups)
        ])

        created = 0

        # Удаляем только книги, которых больше нет в структуре торрента:
        # без глав и чей folder не встречается среди реальных групп.
        actual_folders = {g.get("folder") for g in groups}
        for old in list(release.books):
            if not old.chapters and old.folder not in actual_folders:
                db.delete(old)
        db.flush()
        # после удаления коллекция release.books устарела: удалённые объекты
        # всё ещё в ней, и _upsert_book_from_group может переиспользовать
        # удалённую книгу → при вставке глав будет FOREIGN KEY constraint
        # failed. Перечитываем коллекцию из БД.
        db.expire(release, ["books"])

        for i, group in enumerate(groups):
            book = _upsert_book_from_group(
                db, release, i + 1, group, covers)
            if book is None:
                continue
            created += _append_chapters(book, release, group)
            detect_series_for_book(db, book, release, group)

        for book in release.books:
            book.recompute_status()
        release.recompute_status()

        log.info("release=%s: создано %d глав в %d книгах",
                 release_id, created, len(release.books))
        return created


def _upsert_book_from_group(db: Session, release: Release, ordinal: int,
                            group: dict, covers: CoverService) -> Book | None:
    """Находит или создаёт книгу раздачи по folder, заполняет поля и обложку."""
    title = group.get("title") or release.title
    title_norm = _norm(title)

    book = None
    for b in release.books:
        if (group.get("folder") is None and b.folder is None) or \
                (group.get("folder") is not None and b.folder == group["folder"]):
            book = b
            break

    if book is None:
        book = Book(
            release_id=release.id,
            ordinal=ordinal,
            folder=group.get("folder"),
            title=title,
            title_norm=title_norm,
            title_raw=title,
            status=BookStatus.ANNOUNCED.value,
        )
        db.add(book)
        db.flush()
    else:
        book.ordinal = ordinal
        book.title = title
        book.title_norm = title_norm
        book.title_raw = title

    cover_rel = group.get("cover")

    # хинт о том, где лежит картинка тома внутри торрента — пригодится
    # поллеру, чтобы докачать файл, когда он появится на диске
    if cover_rel:
        book.cover_path_hint = cover_rel

    # своя обложка есть — временная не нужна
    if book.cover_cache_path:
        book.cover_is_temp = False
        return book

    # --- файл уже на диске (повторный ingest после докачки) ---
    if cover_rel:
        cover_abs = settings.downloads_dir / cover_rel
        if cover_abs.exists() and covers.cache_from_torrent(book, cover_abs):
            book.cover_is_temp = False
            return book

    # --- своей обложки нет ---
    # Показываем обложку карточки-раздачи как временную (cover_is_temp=True)
    # до тех пор, пока не получим свою: файл из торрента либо обложку из
    # интернета. Так клиент всегда видит осмысленную картинку, а не заглушку.
    book.cover_is_temp = True

    return book


def _append_chapters(book: Book, release: Release,
                     group: dict) -> int:
    existing = {c.ordinal for c in book.chapters}
    created = 0

    for i, f in enumerate(group["audio"], start=1):
        if i in existing:
            continue

        book.chapters.append(Chapter(
            book_id=book.id,
            release_id=release.id,
            ordinal=i,
            file_index=f["index"],
            rel_path=f["rel_path"],
            size_bytes=f["size_bytes"],
            file_offset=f.get("file_offset", 0),
            status=ChapterStatus.ABSENT.value,
            title=_chapter_title(group, f),
        ))
        created += 1

    return created


def _chapter_title(group: dict, f: dict) -> str:
    stem = f["rel_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    title = group.get("title")
    if title and title not in stem:
        return f"{title} — {stem}"
    return stem


# ============================================================
# ХЕЛПЕРЫ
# ============================================================

def _ensure_source(db: Session):
    from app.db.models import Source
    source = db.execute(
        select(Source).where(Source.name == "rutor.info")
    ).scalar_one_or_none()
    if source is None:
        source = Source(name="rutor.info", base_url=rutor.BASE)
        db.add(source)
        db.flush()
    return source


def _ensure_person(db: Session, name: str) -> Person:
    name_norm = _norm(name)
    person = db.execute(
        select(Person).where(Person.name_norm == name_norm)
    ).scalar_one_or_none()
    if person is None:
        person = Person(name=name, name_norm=name_norm)
        db.add(person)
        db.flush()
    return person


def _ensure_rubric(db: Session, title: str) -> Rubric:
    title_norm = _norm(title)
    rubric = db.execute(
        select(Rubric).where(
            Rubric.kind == "tag",
            Rubric.title_norm == title_norm,
        )
    ).scalar_one_or_none()
    if rubric is None:
        rubric = Rubric(kind="tag", title=title, title_norm=title_norm)
        db.add(rubric)
        db.flush()
    return rubric


def _link_person(db: Session, release: Release, role: str, name: str) -> None:
    if not name:
        return
    person = _ensure_person(db, name)
    for rp in release.persons:
        if rp.role == role and rp.person_id == person.id:
            return
    release.persons.append(ReleasePerson(
        release_id=release.id, person_id=person.id, role=role))


def _replace_person(db: Session, release: Release, role: str,
                    name: str | None) -> None:
    if not name:
        return
    for rp in list(release.persons):
        if rp.role == role:
            release.persons.remove(rp)
    db.flush()
    _link_person(db, release, role, name)


def _link_rubric(db: Session, release: Release, title: str) -> None:
    rubric = _ensure_rubric(db, title)
    for rr in release.rubrics:
        if rr.rubric_id == rubric.id:
            return
    release.rubrics.append(ReleaseRubric(
        release_id=release.id, rubric_id=rubric.id))


def _norm(s: str) -> str:
    return " ".join(s.lower().split())
