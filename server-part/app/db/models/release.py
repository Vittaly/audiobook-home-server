"""
Модель Release — концентратор данных о раздаче с трекера.

Хранит информацию о торренте, карточке (title, description, persons,
rubrics, cover), список файлов. Никакой логики загрузки — только данные.

Содержит методы:
  - find_or_create     — поиск / создание карточки по torrent_hash
  - apply_details      — применить поля с детальной страницы трекера
  - ingest_torrent     — создать книги и главы по структуре торрента
  - add_person         — добавить персону (author/narrator)
  - set_person         — заменить персону (например, чтеца)
  - add_rubric         — добавить рубрику
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Integer, String, Text,
    UniqueConstraint, func, select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.config import settings
from app.db.base import Base
from app.db.models.book_release_link import book_release_link
from app.db.models.cover import Cover
from app.db.models.enums import BookStatus, ContentType, DownloadFileStatus
from app.torrents import structure

if TYPE_CHECKING:
    from app.catalog.covers import CoverService
    from app.db.models.book import Book
    from app.db.models.chapter import Chapter
    from app.db.models.download_file import DownloadFile
    from app.db.models.person import ReleasePerson
    from app.db.models.rubric import ReleaseRubric

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    """Нижний регистр + схлопывание пробелов."""
    return " ".join(s.lower().split())


class Release(Base):
    """
    Карточка раздачи с трекера. Больше не владеет книгами (M:N через
    Поля карточки (title, description, year, persons,
    rubrics, cover, status) живут здесь; статус — агрегат над статусами
    связанных книг.
    """
    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("title_norm", "year", name="ux_release_title_year"),
CheckConstraint(
            "content_type IN ('single','series','collection')",
            name="ck_release_content_type"),
CheckConstraint(
            "status IN ('announced','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_release_status",
        ),
    )

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id:  Mapped[int | None] = mapped_column(Integer, nullable=True)

    torrent_hash: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True)
    magnet:       Mapped[str] = mapped_column(Text, nullable=False)
    source_url:   Mapped[str | None] = mapped_column(Text)
    save_path:    Mapped[str] = mapped_column(Text, nullable=False)

    title:        Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm:   Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    title_raw:    Mapped[str | None] = mapped_column(String(500))
    description:  Mapped[str | None] = mapped_column(Text)
    year:         Mapped[int | None] = mapped_column(Integer, index=True)
    language:     Mapped[str | None] = mapped_column(String(8))

    size_bytes:   Mapped[int | None] = mapped_column(BigInteger)

    content_type: Mapped[str] = mapped_column(
        String(20), default=ContentType.SINGLE.value, nullable=False)
    books_hint:   Mapped[int | None] = mapped_column(Integer)
    folder_map:   Mapped[str | None] = mapped_column(Text)

    status:        Mapped[str] = mapped_column(
        String(20), default=BookStatus.ANNOUNCED.value,
        nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    cover_origin:      Mapped[str | None] = mapped_column(String(20))
    cover_source_url:  Mapped[str | None] = mapped_column(Text)
    cover_cache_path:  Mapped[str | None] = mapped_column(Text)
    cover_mime:        Mapped[str | None] = mapped_column(String(50))
    cover_sha256:      Mapped[str | None] = mapped_column(String(64), index=True)
    cover_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cover_is_temp:     Mapped[bool] = mapped_column(Integer, default=False, nullable=False)

    added_at:      Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)
    released_at:   Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_seen_at:  Mapped[datetime | None] = mapped_column(DateTime)

    # связи
    books: Mapped[list["Book"]] = relationship(
        secondary=book_release_link,
        back_populates="releases",
    )
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="release")
    persons:  Mapped[list["ReleasePerson"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")
    rubrics:  Mapped[list["ReleaseRubric"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")
    download_files: Mapped[list["DownloadFile"]] = relationship(
        back_populates="release", cascade="all, delete-orphan")

    # ================================================================
    # СВОЙСТВА
    # ================================================================

    @property
    def cover(self) -> Cover:
        """Value Object: обложка раздачи."""
        return Cover(self)

    @property
    def release_author(self) -> str:
        """Имя автора для поиска. Пустая строка, если автора нет."""
        for rp in self.persons:
            if rp.role == "author":
                return rp.person.name
        return ""

    @property
    def clean_title(self) -> str:
        """'Автор - Название' → 'Название'. Для поиска в интернете."""
        t = self.title
        if " - " in t:
            return t.split(" - ", 1)[1].strip()
        return t.strip()

    # ================================================================
    # ЗАГРУЗКА ТОРРЕНТА В КЛИЕНТ
    # ================================================================

    async def start_download(self) -> int | None:
        """Добавляет раздачу в торрент клиент, если ещё не добавлена.

        дЕпользует self.save_path как локальное хранилище.
        Идемпотентно: если торрент уже в клиенте — возвращает его
        client_id.

        Returns:
            client_id (torrent_hash) или None при ошиБке.
        """
        from pathlib import Path
        from app.torrents import manager as torrent_manager

        # уже активен — ничего не делаем
        torrent = torrent_manager.get_torrent(self.torrent_hash)
        if torrent is not None:
            log.debug("release=%s: торрент %s уже в клиенте",
                      self.id, self.torrent_hash[:8])
            return self.torrent_hash

        if not self.magnet and not self.source_url:
            log.warning("release=%s: нет magnet/source_url для загрузки", self.id)
            return None

        return await torrent_manager.start_download(self, Path(self.save_path))

    async def start_download_file(self, file_name: str,
                                   priority: int = 1) -> bool:
        """Добавляет торрент в клиент и выставляет приоритет конкретному файлу.

        Ищем file_index:
          1. Среди self.chapters по rel_path (главы).
          2. Если не нашли — среди файлов торрента из клиента (обложка).

        Args:
            file_name: rel_path файла (chapter.rel_path или cover_path_hint).
            priority:  0=skip, 1=low, 2=normal, 3=high.

        Returns:
            True, если торрент добавлен и приоритет выставлен.
        """
        from app.torrents import manager as torrent_manager

        # 1. Ищем file_index среди глав раздачи
        file_index = None
        for ch in self.chapters:
            if ch.rel_path == file_name:
                file_index = ch.file_index
                break

        # 2. Если не глава — возможно файл обложки, ищем в торрент-клиенте
        if file_index is None:
            torrent = torrent_manager.get_torrent(self.torrent_hash)
            if torrent is not None:
                files = torrent_manager.get_files(self.torrent_hash)
                for f in files:
                    if f.name == file_name or f.name.endswith("/" + file_name):
                        file_index = f.index
                        break

        if file_index is None:
            log.warning("release=%s: файл %s не найден среди глав или файлов торрента",
                        self.id, file_name)
            return False

        # Убеждаемся, что торрент в клиенте
        torrent = torrent_manager.get_torrent(self.torrent_hash)
        if torrent is None:
            client_id = await self.start_download()
            if client_id is None:
                log.warning("release=%s: не удалось добавить торрент для файла %s",
                            self.id, file_name)
                return False

        # Выставляем приоритет
        try:
            ok = torrent_manager.set_file_priorities(
                self.torrent_hash,
                high=[file_index] if priority >= 2 else [],
                normal=[file_index] if priority == 1 else [],
                low=[file_index] if priority == 0 else [],
            )
            if ok:
                log.debug("release=%s: приоритет %s для файла %s (#%d)",
                          self.id, priority, file_name, file_index)
            return ok
        except Exception as e:
            log.exception("release=%s: ошибка установки приоритета файла %s",
                          self.id, file_name)
            return False

    # ================================================================
    # АКТУАЛИЗАЦИЯ ФАЙЛОВ ЗАГРУЗКИ (DownloadFile)
    # ================================================================

    def actualize_download_files(self, files_from_client: list) -> bool:
        """
        Актуализирует статусы файлов загрузки по данным торрент-клиента.

        Поллер передаёт перечень файлов из клиента (с их статусами).
        Релиз по именам файлов определяет свои DownloadFile объекты,
        обновляет их статусы и каскадно актуализирует связанные
        объекты (главы → книги, обложки → книги).

        Args:
            files_from_client: список объектов с атрибутами:
                .name (str) — имя файла (rel_path),
                .progress (float),
                .is_complete (bool),
                .downloaded_bytes (int, опционально),
                .pieces_total (int, опционально),
                .pieces_have (int, опционально).

        Returns:
            True, если хотя бы один статус изменился.
        """
        changed = False

        # Индексируем DownloadFile по file_name для быстрого поиска
        df_by_name: dict[str, DownloadFile] = {}
        for df in self.download_files:
            df_by_name[df.file_name] = df

        for f in files_from_client:
            df = df_by_name.get(f.name)
            if df is None:
                continue

            # Обновляем статус DownloadFile по данным клиента
            if df.sync_from_torrent(
                progress=f.progress,
                is_complete=f.is_complete,
                downloaded_bytes=getattr(f, 'downloaded_bytes', None),
                pieces_total=getattr(f, 'pieces_total', None),
                pieces_have=getattr(f, 'pieces_have', None),
            ):
                changed = True
                # Каскад: обновляем статус связанной главы
                self._cascade_from_download_file(df)

        return changed

    def mark_all_download_files_absent(self) -> bool:
        """
        Устанавливает всем DownloadFile раздачи статус ABSENT.

        Вызывается из поллера, когда торрент пропал из клиента.
        Каскадно обновляет статусы связанных глав и книг.

        Returns:
            True, если хотя бы один статус изменился.
        """
        from app.db.models.enums import DownloadFileStatus

        changed = False
        for df in self.download_files:
            if df.status != DownloadFileStatus.ABSENT.value:
                df.status = DownloadFileStatus.ABSENT.value
                df.progress = 0.0
                df.downloaded_bytes = 0
                df.error_message = None
                changed = True
                self._cascade_from_download_file(df)

        return changed

    def _cascade_from_download_file(self, df: "DownloadFile") -> None:
        """
        Каскадно обновляет статусы объектов, связанных с DownloadFile.
        """
        from app.db.models.enums import DownloadFileStatus

        # --- Поиск главы, связанной с этим файлом ---
        for chapter in self.chapters:
            if chapter.download_file_id == df.id:
                if chapter.sync_status_from_download_file():
                    log.debug(
                        "chapter=%s: статус обновлён из download_file %s → %s",
                        chapter.id, df.file_name, chapter.status)
                return

        # --- Поиск книги, у которой это файл обложки ---
        for book in self.books:
            if book.cover_download_file_id == df.id:
                if book.sync_cover_status_from_download_file():
                    log.debug(
                        "book=%s: обложка обновлена из download_file %s → %s",
                        book.book_hash[:12], df.file_name, df.status)
                return

    def find_or_create_download_file(self, db: Session, file_name: str,
                                      size_bytes: int = 0) -> "DownloadFile":
        """
        Находит или создаёт DownloadFile для указанного файла в раздаче.

        Args:
            db: сессия БД.
            file_name: имя файла (rel_path) внутри торрента.
            size_bytes: размер файла.

        Returns:
            Существующий или новый DownloadFile (уже добавлен в сессию).
        """
        from app.db.models.download_file import DownloadFile

        for df in self.download_files:
            if df.file_name == file_name:
                return df

        df = DownloadFile(
            release_id=self.id,
            file_name=file_name,
            size_bytes=size_bytes,
            status=DownloadFileStatus.ABSENT.value,
        )
        db.add(df)
        db.flush()
        self.download_files.append(df)
        return df

    # ================================================================
    # ПОВЕДЕНИЕ
    # ===============================================================

    def recompute_status(self) -> None:
        """Пересчитывает статус карточки по статусам связанных книг."""
        statuses = [b.status for b in self.books]
        new = self._aggregate(statuses)

        if self.status == new:
            return

        self.status = new
        if new == BookStatus.READY.value:
            self.downloaded_at = datetime.utcnow()

    @staticmethod
    def _aggregate(statuses: list[str]) -> str:
        if not statuses:
            return BookStatus.ANNOUNCED.value

        if all(s == BookStatus.READY.value for s in statuses):
            return BookStatus.READY.value
        if all(s == BookStatus.DELETED.value for s in statuses):
            return BookStatus.DELETED.value
        if any(s == BookStatus.ERROR.value for s in statuses):
            return BookStatus.ERROR.value

        has_ready = any(s == BookStatus.READY.value for s in statuses)
        has_in_progress = any(
            s in (BookStatus.DOWNLOADING.value, BookStatus.PARTIAL.value)
            for s in statuses
        )

        if has_in_progress:
            return (BookStatus.PARTIAL.value if has_ready
                    else BookStatus.DOWNLOADING.value)
        if has_ready:
            return BookStatus.PARTIAL.value
        return BookStatus.ANNOUNCED.value

    # ================================================================
    # UPSERT КАРТОЧКИ
    # ================================================================

    @classmethod
    def find_or_create(cls, db: Session, item: dict) -> "Release | None":
        """
        Находит или создаёт карточку раздачи по нормализованному item
        (из rutor.fetch_search_page / crawler).

        Args:
            db: Сессия БД.
            item: Словарь с ключами: torrent_hash, title, magnet, content_type,
                  source_url, size_bytes, books_hint, author, narrator, title_raw,
                  added_at, year.

        Returns:
            Release или None (если нет torrent_hash — раздача непригодна).
        """
        from app.db.models import Source

        torrent_hash = item["torrent_hash"]

        release = db.execute(
            select(cls).where(cls.torrent_hash == torrent_hash)
        ).scalar_one_or_none()

        if release is not None:
            release.last_seen_at = datetime.now(timezone.utc)
            if release.released_at is None and item.get("added_at"):
                release.released_at = Release._parse_tracker_date(item["added_at"])
            return release

        # Раздача без torrent_hash непригодна для скачивания
        if not torrent_hash:
            title_raw = item.get("title_raw") or item.get("title") or "?"
            log.warning("find_or_create: пропуск раздачи без torrent_hash: %s",
                        title_raw)
            return None

        title = item["title"]
        title_norm = _norm(title)
        year = item.get("year")

        # Дедикация по title+year (для повторов с разными torrent_hash)
        release = db.execute(
            select(cls).where(
                cls.title_norm == title_norm,
                cls.year == year,
            )
        ).scalar_one_or_none()

        if release is None:
            release = cls(
                title=title,
                title_norm=title_norm,
                title_raw=item.get("title_raw"),
                year=year,
                status=BookStatus.ANNOUNCED.value,
                torrent_hash=torrent_hash,
                magnet=item["magnet"],
                save_path=str(settings.downloads_dir / _norm(title)),
                content_type=item["content_type"],
                released_at=Release._parse_tracker_date(item.get("added_at")),
            )
            db.add(release)
            db.flush()

            if item.get("author"):
                release.add_person(db, "author", item["author"])
            if item.get("narrator"):
                release.add_person(db, "narrator", item["narrator"])

        # Актуализация полей при каждом проходе
        source = db.execute(
            select(Source).where(Source.name == "rutor.info")
        ).scalar_one_or_none()
        if source is None:
            source = Source(
                name="rutor.info",
                base_url=settings.RUTOR_BASE_URL,
            )
            db.add(source)
            db.flush()
        release.source_id = source.id

        release.torrent_hash = torrent_hash
        release.magnet = item["magnet"]
        release.source_url = item.get("source_url")
        release.save_path = str(settings.downloads_dir / _norm(title))
        release.title_raw = item.get("title_raw")
        release.size_bytes = item.get("size_bytes")
        release.content_type = item["content_type"]
        release.books_hint = item["books_hint"]
        release.last_seen_at = datetime.now(timezone.utc)
        db.flush()

        return release

    # ================================================================
    # ПРИМЕНЕНИЕ UPDATE ИЗ ДЕТАЛЬНОЙ СТРАНИЦЫ
    # ================================================================

    def apply_details(self, db: Session, update: dict) -> None:
        """
        Применяет поля с детальной страницы трекера (rutor.fetch_torrent_details).

        НЕ трогает title и author: заголовок раздачи надёжнее полей формы.
        Поля формы используем только для того, чего нет в заголовке.
        """
        if update.get("description"):
            self.description = update["description"]

        # year уточняем только если в поисковой выдаче его не было
        if update.get("year") and self.year is None:
            self.year = update["year"]

        # чтец — отдельное поле, ему доверяем
        if update.get("narrator"):
            self.set_person(db, "narrator", update["narrator"])

        # рубрики
        for tag in update.get("rubrics") or []:
            self.add_rubric(db, tag)

        # обложка-URL: только если ещё не установлена
        if not self.cover_source_url and update.get("cover_url"):
            self.cover_source_url = update["cover_url"]
            self.cover_origin = "tracker"

        # content_type
        if update.get("content_type"):
            self.content_type = update["content_type"]
            if update.get("books_hint"):
                self.books_hint = update["books_hint"]

    # ================================================================
    # НАПОЛНЕНИЕ КНИГ ИЗ ТОРРЕНТА
    # ================================================================

    def ingest_torrent(self, db: Session, files: list[dict],
                       torrent_name: str, covers: "CoverService") -> int:
        """
        Создаёт книги внутри раздачи по фактической структуре торрента.

        Для каждой группы-книги:
          - находит или создаёт Book через Book.find_or_create();
          - привязывает книгу к раздаче (link_release);
          - создаёт главы (book.ingest_chapters);
          - определяет серию (book.detect_series);
          - обрабатывает обложку тома.

        Возвращает количество созданных глав.
        """
        from app.db.models.book import Book

        groups = structure.split_torrent_into_books(files, torrent_name)
        if not groups:
            return 0

        is_multi = len(groups) > 1

        if is_multi and self.content_type == "single":
            self.content_type = "series"
            self.books_hint = len(groups)

        self.folder_map = json.dumps([
            {"folder": g["folder"], "title": g["title"], "order": i + 1}
            for i, g in enumerate(groups)
        ])

        created = 0

        # ВАЖНО: удаление книг-заглушек ПОСЛЕ upsert
        for i, group in enumerate(groups):
            book = self._upsert_book_from_group(db, group, covers)
            if book is None:
                continue
            created += book.ingest_chapters(self, group)
            book.detect_series(db, self, group)

            # Создаём DownloadFile для каждой главы этой книги
            for chapter in book.chapters:
                if chapter.release_id == self.id and chapter.download_file_id is None:
                    df = self.find_or_create_download_file(
                        db, chapter.rel_path, chapter.size_bytes)
                    chapter.download_file_id = df.id

            # Создаём DownloadFile для обложки тома, если есть hint
            cover_rel = group.get("cover")
            if cover_rel and book.cover_download_file_id is None:
                df = self.find_or_create_download_file(db, cover_rel, 0)
                book.cover_download_file_id = df.id

        # удаляем «книги-заглушки», оставшиеся без глав
        live_books = [b for b in self.books if b.chapters]
        stale_books = [b for b in self.books if not b.chapters]
        for old in stale_books:
            db.delete(old)
        db.flush()

        # статусы считаем только по живым книгам
        for book in live_books:
            book.recompute_status()
        self.recompute_status()

        log.info("release=%s: создано %d глав в %d книгах",
                 self.id, created, len(self.books))
        return created

    def _upsert_book_from_group(self, db: Session, group: dict,
                                covers: "CoverService") -> "Book | None":
        """Находит или создаёт книгу раздачи по folder, заполняет поля и обложку."""
        from app.db.models.book import Book, compute_book_hash

        title = group.get("title") or self.title
        title_norm = _norm(title)

        # Поиск среди уже привязанных книг по folder
        book = None
        for b in self.books:
            if (group.get("folder") is None and b.folder is None) or \
                    (group.get("folder") is not None and b.folder == group["folder"]):
                book = b
                break

        if book is None:
            # Создаём новую книгу через Book.find_or_create
            author = self.release_author
            book_hash = compute_book_hash(
                author=author,
                title=title,
                year=self.year,
            )
            book = Book.find_or_create(
                db, book_hash,
                ordinal=(group.get("order") or 1),
                folder=group.get("folder"),
                title=title,
                title_norm=title_norm,
                title_raw=title,
                status=BookStatus.ANNOUNCED.value,
            )
            # Привязываем к раздаче
            book.link_release(self)
            db.flush()
        else:
            book.title = title
            book.title_norm = title_norm
            book.title_raw = title

        # --- Обложка тома ---
        cover_rel = group.get("cover")

        # Хинт о том, где лежит картинка тома внутри торрента
        if cover_rel:
            book.cover_path_hint = cover_rel

        # Своя обложка уже есть — временная не нужна
        if book.cover_cache_path:
            book.cover_is_temp = False
            return book

        # Файл уже на диске (повторный ingest после докачки)
        if cover_rel:
            cover_abs = settings.downloads_dir / cover_rel
            if cover_abs.exists() and book.cover.copy_from_torrent(cover_abs, covers):
                book.cover_is_temp = False
                return book

        # Своей обложки нет — показываем обложку карточки как временную
        book.cover_is_temp = True

        return book

    # ================================================================
    # ПЕРСОНЫ И РУБРИКИ
    # ================================================================

    def add_person(self, db: Session, role: str, name: str) -> None:
        """Добавляет персону (author/narrator) к карточке раздачи."""
        from app.db.models.person import Person, ReleasePerson

        person = Person.find_or_create(db, name)

        # Не дублируем ту же роль
        for rp in self.persons:
            if rp.role == role and rp.person_id == person.id:
                return

        rp = ReleasePerson(release_id=self.id, person_id=person.id, role=role)
        db.add(rp)
        db.flush()

    def set_person(self, db: Session, role: str, name: str) -> None:
        """Заменяет персону с заданной ролью (например, чтеца)."""
        from app.db.models.person import Person, ReleasePerson

        person = Person.find_or_create(db, name)

        # Удаляем старые записи с этой ролью
        for rp in list(self.persons):
            if rp.role == role:
                db.delete(rp)

        rp = ReleasePerson(release_id=self.id, person_id=person.id, role=role)
        db.add(rp)
        db.flush()

    def add_rubric(self, db: Session, title: str) -> None:
        """Добавляет рубрику (жанр/тег) к карточке раздачи."""
        from app.db.models.rubric import Rubric, ReleaseRubric

        rubric = Rubric.find_or_create(db, kind="tag", title=title)

        # Не дублируем
        for rr in self.rubrics:
            if rr.rubric_id == rubric.id:
                return

        rr = ReleaseRubric(release_id=self.id, rubric_id=rubric.id)
        db.add(rr)
        db.flush()

    # ================================================================
    # ХЕЛПЕРЫ
    # ================================================================

    @staticmethod
    def _parse_tracker_date(value: str | None) -> datetime:
        """Дата создания раздачи на трекере ('YYYY-MM-DD') -> datetime (UTC)."""
        if value:
            try:
                return datetime.strptime(value, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                log.warning("release: некорректная дата трекера: %r", value)
        return datetime.now(timezone.utc)
