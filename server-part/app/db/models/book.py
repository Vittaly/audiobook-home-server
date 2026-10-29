"""
Модель Book — центральная сущность каталога.

Содержит бизнес-логику в виде методов:
  - find_or_create     — поиск / создание книги по book_hash
  - link_release       — привязка к раздаче
  - ingest_chapters    — создание глав по группе файлов
  - sync_chapters      — догон статусов глав по данным торрента
  - detect_series      — определение серии книги
  - needs_online_cover — нужно ли искать обложку в интернете
  - apply_temp_cover   — пометить обложку временной
  - start_download     — запустить загрузку книги
  - stop_download      — остановить загрузку
  - remove_download    — удалить торрент с файлами
  - fast_download      — быстрый старт (обложка + 1-я глава)
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
    select,
)
from app.config import settings
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.catalog.types import DownloadResult
from app.db.base import Base
from app.db.models.book_release_link import book_release_link
from app.db.models.cover import Cover
from app.db.models.enums import BookStatus, ChapterStatus

if TYPE_CHECKING:
    from app.catalog.covers import CoverService
    from app.db.models.chapter import Chapter
    from app.db.models.release import Release
    from app.db.models.series import Series

log = logging.getLogger(__name__)

# Серии, которые не являются литературными — слова-маркеры
_NOT_SERIES_WORDS = re.compile(
    r"\b(сборник|anthology|цикл|series|collection)\b", re.IGNORECASE)

# Паттерн "Том N" / "Часть N" / "Книга N" в заголовке раздачи
_TOME_IN_TITLE_RE = re.compile(
    r"""
    (?:том|часть|volume|part|книга|book)\s
    (\d{1,4})
    """,
    re.IGNORECASE | re.VERBOSE,
)


def compute_book_hash(
    author: str,
    title: str,
    year: int | None = None,
    series_norm: str | None = None,
    series_order: int | None = None,
) -> str:
    """
    SHA256-хэш от комбинации: автор | название | year | series_norm | series_order.
    Все компоненты приводятся к нижнему регистру и trim'ятся.
    """
    import hashlib

    key = (
        f"{author.lower().strip()}|{title.lower().strip()}|"
        f"{year or ''}|{series_norm or ''}|{series_order or 0}"
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _norm(s: str) -> str:
    """Нижний регистр + схлопывание пробелов."""
    return " ".join(s.lower().split())


# ============================================================
# ФУНКЦИЯ-ХЕЛПЕР (нужна для chapter title, без self)
# ============================================================

def _chapter_title(group: dict, f: dict) -> str:
    stem = f["rel_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    title = group.get("title")
    if title and title not in stem:
        return f"{title} — {stem}"
    return stem


# ============================================================
# МОДЕЛЬ
# ============================================================


class Book(Base):
    """
    Книга (том/произведение) — центральная сущность каталога.
    Идентифицируется book_hash (SHA256 от автор+название+year+series_norm+series_order).
    Связана с одной или несколькими раздачами (Release) через M:N book_release_link.
    """
    __tablename__ = "books"
    __table_args__ = (
        CheckConstraint(
            "status IN ('announced','downloading','partial','ready',"
            "'deleting','deleted','error')",
            name="ck_book_status",
        ),
    )

    book_hash: Mapped[str] = mapped_column(
        String(64), primary_key=True)

    title:      Mapped[str] = mapped_column(String(500), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    title_raw:  Mapped[str | None] = mapped_column(String(500))
    author:     Mapped[str | None] = mapped_column(String(500))
    narrator:   Mapped[str | None] = mapped_column(String(500))
    year:       Mapped[int | None] = mapped_column(Integer, index=True)

    ordinal:    Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    folder:     Mapped[str | None] = mapped_column(Text)

    series_id:    Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), nullable=True)
    series_order: Mapped[int | None] = mapped_column(Integer)

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
    cover_path_hint:   Mapped[str | None] = mapped_column(Text)

    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    # признак «книга ещё не полностью определена»: данные книги (главы,
    # описание) могут измениться; снимается после получения глав/описания.
    is_provisional: Mapped[bool] = mapped_column(
        Integer, default=False, nullable=False)

    # M:N связи
    releases: Mapped[list["Release"]] = relationship(
        secondary=book_release_link,
        back_populates="books",
    )
    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan",
        order_by="Chapter.ordinal")
    series: Mapped["Series | None"] = relationship(back_populates="books")

    # ---------- поведение ----------

    def recompute_status(self) -> None:
        """Пересчитывает статус книги по главам."""
        statuses = [c.status for c in self.chapters]
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

        if all(s == ChapterStatus.READY.value for s in statuses):
            return BookStatus.READY.value
        if all(s == ChapterStatus.DELETED.value for s in statuses):
            return BookStatus.DELETED.value
        if any(s == ChapterStatus.ERROR.value for s in statuses):
            return BookStatus.ERROR.value

        has_ready = any(s == ChapterStatus.READY.value for s in statuses)
        has_in_progress = any(
            s in (ChapterStatus.DOWNLOADING.value, ChapterStatus.PARTIAL.value)
            for s in statuses
        )

        if has_in_progress:
            return (BookStatus.PARTIAL.value if has_ready
                    else BookStatus.DOWNLOADING.value)
        if has_ready:
            return BookStatus.PARTIAL.value
        return BookStatus.ANNOUNCED.value

    # ============================================================
    # Cover accessor
    # ============================================================

    @property
    def cover(self) -> Cover:
        """
        Cover Value Object для этой книги.
        Позволяет писать: book.cover.start_download(...), book.cover.is_settled и т.д.
        """
        return Cover(self)

    # ============================================================
    # POJO helpers (не требуют сессии, только поля self)
    # ============================================================

    @property
    def needs_online_cover(self) -> bool:
        """
        Нужен ли книге онлайн-поиск обложки.

        Да, когда:
        - нет своей обложки (cover_cache_path is None),
        - обложка временная (cover_is_temp),
        - не превышен лимит ретраев,
        - книга часть многотомной раздачи (у однокнижных обложкой
          занимается уровень раздачи),
        - файл-картинка из торрента либо не заявлен (нет hint),
          либо книга готова, а файла так и не появилось.
        """
        if self.cover_cache_path is not None or not self.cover_is_temp:
            return False

        from app.config import settings

        # логика из poller._book_needs_online_cover
        if (self.cover_retry_count or 0) >= settings.COVER_RETRY_LIMIT:
            return False

        # для однокнижных раздач обложкой занимается уровень раздачи
        if len(self.releases) == 0:
            return False
        # берём первую раздачу для проверки числа книг
        first_release = self.releases[0]
        if len(first_release.books) <= 1:
            return False

        # если у книги нет hint'а на файл-картинку — ищем онлайн
        cover_path_hint = getattr(self, "cover_path_hint", None)
        if not cover_path_hint:
            return True

        # ждём, пока файл картинки докачается вместе с главой
        if self.status != BookStatus.READY.value:
            return False

        hint_path = settings.downloads_dir / cover_path_hint
        if hint_path.exists():
            return False

        # книга готова, а файла картинки нет — его нет в раздаче
        return True

    def apply_temp_cover(self) -> None:
        """При старте загрузки включает признак временной обложки.

        Если у книги уже есть своя обложка — сбрасывает флаг временной.
        Иначе включает, чтобы клиент показывал обложку карточки-раздачи,
        пока файл не докачается или не найдётся онлайн.
        """
        if self.cover_cache_path:
            self.cover_is_temp = False
        else:
            self.cover_is_temp = True

    # ============================================================
    # CRUD-like methods (требуется db Session)
    # ============================================================

    @classmethod
    def find_or_create(cls, db: Session, book_hash: str, **defaults) -> "Book":
        """Находит книгу по book_hash или создаёт новую.

        Args:
            db:         открытая сессия
            book_hash:  первичный ключ
            defaults:   поля для создания книги (title, title_norm, author, …)

        Returns:
            Существующий или новый экземпляр Book (уже добавлен в сессию).
        """
        book = db.get(cls, book_hash)
        if book is None:
            book = cls(book_hash=book_hash, **defaults)
            db.add(book)
            db.flush()
            log.info("book: создана новая %s (%s)", book_hash[:12], defaults.get("title", ""))
        return book

    def link_release(self, release: "Release") -> None:
        """Привязывает книгу к раздаче (M:N).

        Идемпотентно: если связь уже есть — ничего не делает.
        """
        if release not in self.releases:
            self.releases.append(release)

    def ingest_chapters(self, release: "Release", group: dict) -> int:
        """Создаёт главы книги по группе файлов из торрента.

        Args:
            release: раздача-владелец (передаётся для release_id в Chapter).
            group:   результат split_torrent_into_books — словарь с 'audio'.

        Returns:
            Количество созданных глав.
        """
        existing = {c.ordinal for c in self.chapters}
        created = 0

        for i, f in enumerate(group.get("audio", []), start=1):
            if i in existing:
                continue

            from app.db.models.chapter import Chapter

            self.chapters.append(Chapter(
                book_hash=self.book_hash,
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

    def sync_chapters(self, files_by_idx: dict) -> bool:
        """Догоняет статусы глав по данным торрента.

        Args:
            files_by_idx: {file_index: TorrentFileInfo} по всей раздаче.

        Returns:
            True, если хотя бы один статус изменился.
        """
        changed = False

        for chapter in self.chapters:
            f = files_by_idx.get(chapter.file_index)
            if f is None:
                continue

            progress = float(f.progress)

            if f.is_complete or progress >= 1.0:
                if chapter.status != ChapterStatus.READY.value:
                    chapter.mark_ready()
                    changed = True
            elif progress > 0:
                if chapter.status in (
                    ChapterStatus.ABSENT.value,
                    ChapterStatus.DOWNLOADING.value,
                ) or abs((chapter.progress or 0.0) - progress) > 0.01:
                    chapter.mark_progress(progress)
                    changed = True

        if changed:
            self.recompute_status()

        return changed

    def find_missing_peers(self, db: Session) -> list[tuple[str, int]]:
        """
        Возвращает список (series_name, volume) недостающих книг серии.

        Учитывает только книги, у которых series_id уже установлен
        (после detect_series). Проверяет каталог — если книга с таким
        series_id + series_order уже существует, она не включается в результат.

        Returns:
            Список (series_name, volume) для недостающих книг.
            Пустой список, если серия не определена или все тома уже есть.
        """
        if self.series_id is None or self.series_order is None:
            return []

        from app.db.models.series import Series

        # Загружаем название серии
        series = db.get(Series, self.series_id)
        if series is None:
            return []
        series_name = series.title

        # Все книги этой серии в каталоге
        existing = {
            b.series_order
            for b in db.execute(
                select(Book).where(
                    Book.series_id == self.series_id,
                    Book.series_order.isnot(None),
                )
            ).scalars().all()
            if b.book_hash != self.book_hash  # исключаем себя
        }

        missing: list[tuple[str, int]] = []
        for vol in range(1, self.series_order):
            if vol not in existing:
                missing.append((series_name, vol))

        if missing:
            log.info("book=%s: в серии %r не хватает томов %s",
                      self.book_hash[:12], series_name,
                      [v for _, v in missing])

        return missing

    def detect_series(self, db: Session, release: "Release", group: dict) -> None:
        """Определяет серию книги и привязывает к Series.

        Стратегия (из series_detect.detect_series_for_book):
        1. Берёт series_hint/volume из group (результат парсинга hidewrap).
        2. Если нет — пробует извлечь серию из заголовка раздачи
           (только для однокнижных раздач).
        3. Если найдено — создаёт Series (find_or_create) и привязывает.
        """
        series_name: str | None = group.get("series_hint")
        volume: int | None = group.get("volume")

        # --- 2. Из заголовка раздачи ---
        if not series_name:
            single = len(release.books) == 1
            if single or group.get("title") is None:
                name_from_title, vol_from_title = self._series_from_release_title(release)
                if name_from_title:
                    series_name = name_from_title
                    volume = volume or vol_from_title

        if not series_name:
            return

        series_name = self._clean_series_name(series_name)
        if not series_name or _NOT_SERIES_WORDS.search(series_name):
            return

        # --- 3. Создаём / находим Series ---
        from app.db.models.series import Series

        series = Series.find_or_create(db, name=series_name)
        self.series_id = series.id
        if volume is not None:
            self.series_order = volume

        log.info("book=%s: серия определена — %r (том %s)",
                 self.book_hash[:12], series.title, volume)

    @staticmethod
    def _series_from_release_title(release: "Release") -> tuple[str | None, int | None]:
        """'Сергей Лукьяненко - Дозоры. Том 1' → ('Дозоры', 1)."""
        title = release.title or ""
        m = _TOME_IN_TITLE_RE.search(title)
        if not m:
            return None, None

        volume = int(m.group(1))
        head = title[:m.start()].strip(" .-–—,;:")

        # убираем автора «Автор - Название»
        author = release.release_author
        if author and head.lower().startswith(author.lower()):
            head = head[len(author):].strip(" -–—.:,")

        head = head.strip(" .-–—,;:")
        if len(head) < 2:
            return None, None
        return head, volume

    @staticmethod
    def _clean_series_name(name: str) -> str:
        name = name.strip(" .-–—,;:\"'«»()[]")
        return re.sub(r"\s+", " ", name).strip()

    # ============================================================
    # Загрузка
    # ============================================================

    async def start_download(
        self,
        db: Session,
        covers: "CoverService | None" = None,
    ) -> DownloadResult:
        """Запускает загрузку книги через главы.

        Делегирует:
        1. Релизу — добавление торрента в клиент (release.start_download).
        2. Главам — установку приоритетов (chapter.start_download).
        3. Себе — обновление статусов, активность.

        Книга НЕ знает про downloads_dir / save_path — это ответственность Release.

        Args:
            db:     открытая сессия (caller отвечает за commit).
            covers: CoverService (опционально, для обложки).

        Returns:
            DownloadResult с результатом операции.
        """
        from app.lifecycle import activity

        # --- проверка раздачи ---
        release = self.releases[0] if self.releases else None
        if release is None:
            return DownloadResult(
                success=False, error_code="no_release",
                error_message="у книги нет раздачи-источника")

        if self.status == BookStatus.READY.value:
            return DownloadResult(
                success=False, release_id=release.id,
                error_code="already_done",
                error_message="книга уже скачана")

        # --- релиз добавляет торрент ---
        client_id = await release.start_download()
        if client_id is None:
            return DownloadResult(
                success=False, release_id=release.id,
                error_code="client_failed",
                error_message="не удалось добавить раздачу в торрент-клиент")

        # --- статусы ---
        self.status = BookStatus.DOWNLOADING.value
        self.apply_temp_cover()
        activity.touch(db, self.book_hash, "download")
        db.commit()

        # --- приоритеты главам (книга делегирует главам) ---
        for i, chapter in enumerate(self.chapters):
            if i == 0:
                await chapter.start_download(priority=3)  # высший — первая глава
            else:
                await chapter.start_download(priority=1)  # низкий — остальные

        log.info("book=%s: старт загрузки, release=%s, chapters=%d",
                 self.book_hash[:12], release.id, len(self.chapters))

        return DownloadResult(
            success=True, release_id=release.id, book_hash=self.book_hash,
            client_id=client_id, status=self.status,
            save_path=release.save_path,
        )

    # ============================================================
    # STOP
    # ============================================================

    def stop_download(
        self,
        db: Session,
        delete_files: bool = False,
    ) -> DownloadResult:
        """Останавливает загрузку книги.

        Args:
            db:           открытая сессия (caller отвечает за commit).
            delete_files: удалять ли файлы с диска.

        Returns:
            DownloadResult с результатом операции.
        """
        from app.lifecycle import activity
        from app.torrents import manager

        release = self.releases[0] if self.releases else None
        if release is None:
            return DownloadResult(
                success=False, error_code="no_release",
                error_message="у книги нет раздачи-источника")

        ok = manager.remove(release.torrent_hash, delete_files=delete_files)
        if not ok:
            log.warning("book=%s: торрент %s не найден в клиенте",
                        self.book_hash[:12], release.torrent_hash[:8])

        if delete_files:
            for b in release.books:
                b.downloaded_at = None
                for chapter in b.chapters:
                    chapter.status = ChapterStatus.ABSENT.value
                    chapter.progress = 0.0
                    chapter.downloaded_bytes = 0
                b.recompute_status()
            release.recompute_status()

        db.commit()

        return DownloadResult(
            success=True, book_hash=self.book_hash, status=self.status)

    # ============================================================
    # REMOVE (DELETE)
    # ============================================================

    def remove_download(
        self,
        db: Session,
    ) -> DownloadResult:
        """Удаляет торрент с файлами с диска. Метаданные книги сохраняются.

        Args:
            db: открытая сессия (caller отвечает за commit).

        Returns:
            DownloadResult с результатом операции.
        """
        from app.lifecycle import activity
        from app.torrents import manager

        release = self.releases[0] if self.releases else None
        if release is None:
            return DownloadResult(
                success=False, error_code="no_release",
                error_message="у книги нет раздачи-источника")

        removed = manager.remove(release.torrent_hash, delete_files=True)
        log.info("book=%s: удалён торрент раздачи %s (ok=%s)",
                 self.book_hash[:12], release.torrent_hash[:8], removed)

        # папка торрента
        folder = self._release_torrent_folder(release)
        if folder is not None and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            log.info("release=%s: удалена папка %s", release.id, folder)

        # сброс глав всех книг раздачи
        for b in release.books:
            b.downloaded_at = None
            for chapter in b.chapters:
                chapter.status = ChapterStatus.DELETED.value
                chapter.progress = 0.0
                chapter.downloaded_bytes = 0
                chapter.pieces_have = 0
            b.recompute_status()
        release.recompute_status()

        activity.touch(db, self.book_hash, "delete")
        db.commit()

        return DownloadResult(
            success=True, book_hash=self.book_hash, status=self.status)

    @staticmethod
    def _release_torrent_folder(release: "Release") -> Path | None:
        """Корневая папка торрента внутри downloads_dir (из первой главы)."""
        for b in release.books:
            for chapter in b.chapters:
                parts = Path(chapter.rel_path).parts
                if parts:
                    return settings.downloads_dir / parts[0]
        return None

    async def fast_download(
        self,
        covers: "CoverService",
        db: Session | None = None,
    ) -> bool:
        """Быстрый старт: обложка + первая глава сразу.

        Последовательность:
        1. Загружает обложку (если ещё нет).
        2. Запускает загрузку первой главы.
        3. Остальные главы загрузятся в фоне (поллер).

        Args:
            covers: CoverService для скачивания обложки.
            db:     опционально — если передана, делает commit.

        Returns:
            True, если хотя бы обложка или первая глава запущены.
        """
        did_something = False

        # --- 1. Обложка ---
        if not self.cover.is_settled:
            release = self.releases[0] if self.releases else None
            author = self.author or ""
            rubrics: list[str] | None = None

            if release:
                # если у книги нет автора — берём из раздачи
                if not author:
                    author = release.release_author or ""
                rubrics = [rp.rubric.title for rp in release.rubrics]

            title = self.title or (release.title if release else "")
            url = release.cover_source_url if (release and release.cover_source_url) else None

            ok = await self.cover.start_download(
                title=title, author=author, rubrics=rubrics,
                url=url, covers=covers,
            )
            if ok:
                did_something = True

        # --- 2. Первая глава — высокий приоритет ---
        if self.chapters and self.chapters[0].status != ChapterStatus.READY.value:
            ok = await self.chapters[0].start_download(priority=2)
            if ok:
                did_something = True

        return did_something
