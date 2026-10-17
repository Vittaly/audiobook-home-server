# -*- coding: utf-8 -*-
"""
Сквозной интеграционный тест многокнижной раздачи.

Сценарий:
  1. Изолированная БД (временный DATA_DIR) до импорта app.config.
  2. Создание карточки раздачи Release + автор/чтец/рубрики.
  3. Синтетический торрент с двумя томами («Ночной Дозор. Том 1/2»)
     по 2 mp3 и обложку cover.jpg в каждом томе.
  4. ingest_release → 2 книги, локальные ordinals глав, серия, обложки.
  5. Сервисный слой: list_releases / get_release / list_books / get_book /
     get_download_status.
  6. HTTP-слой: /releases, /releases/{id}, /releases/{id}/cover,
     /books, /books/{id}, /books/{id}/cover, /books/{id}/status.
  7. Сценарий временной обложки книги (многотомная раздача без обложек
     в торренте): cover_is_temp=True при ingest, download.start() её
     подтверждает, /books/{id}/cover отдаёт обложку раздачи с
     X-Cover-Temp: true и Cache-Control: no-cache; онлайн-поиск по
     названию книги снимает temp.
  8. Сценарий докачки обложки тома по cover_path_hint: файл отсутствовал
     на диске при ingest → hint сохранён, cover_is_temp=True; после
     появления файла poller._try_copy_book_cover копирует его в кэш и
     снимает temp.

Запуск:  venv\\Scripts\\python.exe _test_multi_book.py
"""

import asyncio
import os
import sys
import tempfile
import unicodedata
from pathlib import Path

# Корень проекта — чтобы запускать скрипт и из tests/, и из корня
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ВАЖНО: ставим DATA_DIR ДО импорта app.config (engine binds db_url при импорте)
_TMP = tempfile.mkdtemp(prefix="abs_mb_")
os.environ["DATA_DIR"] = _TMP
os.environ["SCRAPE_RUN_ON_START"] = "false"
os.environ["MDNS_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

from unittest.mock import AsyncMock, patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.catalog import covers, download, service  # noqa: E402
from app.catalog.book_covers import try_search_book_cover  # noqa: E402
from app.catalog.cover_search import CoverSearchResult  # noqa: E402
from app.catalog.ingest import ingest_release  # noqa: E402
from app.config import settings  # noqa: E402
from app.torrents import poller  # noqa: E402
from app.db.models import (  # noqa: E402
    Book,
    Chapter,
    Person,
    Release,
    ReleasePerson,
    ReleaseRubric,
    Rubric,
    Series,
)
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.api.server import create_app  # noqa: E402


def norm(text: str) -> str:
    """Упрощённая нормализация: lowercase + без диакритики."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def make_files(torrent_root: str, folders: list[str], files_per_book: int,
               include_cover: bool = True):
    """Синтетический список файлов торрента (структура structure.py)."""
    files = []
    idx = 0
    for folder in folders:
        for n in range(1, files_per_book + 1):
            files.append({
                "index": idx,
                "rel_path": f"{torrent_root}/{folder}/{n:02d}.mp3",
                "size_bytes": 1024 * (n + 1),
                "file_offset": 0,
            })
            idx += 1
        if include_cover:
            # обложка тома
            files.append({
                "index": idx,
                "rel_path": f"{torrent_root}/{folder}/cover.jpg",
                "size_bytes": 4096,
                "file_offset": 0,
            })
            idx += 1
    return files


# минимальный JPEG-массив, достаточный для CoverService (_store не валидирует)
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x01\x02" * 64 + b"\xff\xd9"


class _FakeResp:
    """Имитация httpx.Response для patch'а AsyncClient.get."""
    headers = {"content-type": "image/jpeg"}
    content = _JPEG

    def raise_for_status(self):
        pass


async def _search_ok(title, author, rubrics=None) -> CoverSearchResult:
    """Мок search_cover — всегда находит обложку."""
    return CoverSearchResult(url="https://example.com/cover.jpg", source="test")


def main() -> None:
    init_db()
    print(f"DATA_DIR = {settings.data_dir}")

    # --- 2. карточка раздачи ------------------------------------------------
    torrent_name = "Ночной Дозор - сборник (2004)"
    title = "Ночной Дозор - сборник"
    db = SessionLocal()
    try:
        release = Release(
            torrent_hash="f" * 40,
            magnet="magnet:?xt=urn:btih:" + "f" * 40,
            source_url="https://rutor.info/torrent/12345",
            save_path=str(settings.downloads_dir),
            title=title,
            title_norm=norm(title),
            title_raw="Ночной Дозор - сборник (2004)",
            description="Два первых тома цикла",
            language="ru",
            size_bytes=6 * 1024,
            content_type="single",
        )
        db.add(release)
        db.flush()

        author = Person(name="Сергей Лукьяненко", name_norm=norm("Сергей Лукьяненко"))
        db.add(author)
        db.flush()
        db.add(ReleasePerson(release_id=release.id, person_id=author.id, role="author"))

        narrator = Person(name="Иван Иванов", name_norm=norm("Иван Иванов"))
        db.add(narrator)
        db.flush()
        db.add(ReleasePerson(release_id=release.id, person_id=narrator.id, role="narrator"))

        rubric = Rubric(kind="genre", title="Фантастика",
                        title_norm=norm("Фантастика"))
        db.add(rubric)
        db.flush()
        db.add(ReleaseRubric(release_id=release.id, rubric_id=rubric.id))

        release_id = release.id
        db.commit()
    finally:
        db.close()

    # --- 3. физические файлы торрента (обложки томов) ----------------------
    downloads = settings.downloads_dir
    downloads.mkdir(parents=True, exist_ok=True)
    folders = ["Ночной Дозор. Том 1", "Ночной Дозор. Том 2"]
    for folder in folders:
        d = downloads / torrent_name / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0_synthetic_jpeg_" + folder.encode("utf-8"))

    files = make_files(torrent_name, folders, files_per_book=2)
    cover_svc = covers.CoverService()

    # --- 4. ingest ----------------------------------------------------------
    created = ingest_release(release_id, files, torrent_name, cover_svc)
    print(f"ingest_release: создано глав = {created}")
    assert created == 4, f"ожидалось 4 главы, получено {created}"

    db = SessionLocal()
    try:
        rel = db.get(Release, release_id)
        assert rel is not None
        assert rel.content_type == "series", f"content_type={rel.content_type}"
        assert rel.books_hint == 2, f"books_hint={rel.books_hint}"

        import json as _json
        folder_map = _json.loads(rel.folder_map or "[]")
        assert len(folder_map) == 2, f"folder_map={folder_map}"

        books = list(rel.books)
        assert len(books) == 2, f"книг: {len(books)}"
        for i, b in enumerate(books, start=1):
            assert b.ordinal == i, f"ordinal={b.ordinal} (ожидалось {i})"
            assert b.folder == folders[i - 1], f"folder={b.folder!r}"
            assert len(b.chapters) == 2, \
                f"книга {i}: глав {len(b.chapters)}"
            assert [c.ordinal for c in b.chapters] == [1, 2], \
                f"книга {i}: local ordinals глав {[c.ordinal for c in b.chapters]}"
            assert b.series_id is not None, f"книга {i}: серия не определена"
            assert b.series_order == i, f"книга {i}: series_order={b.series_order}"
            # обложка тома закэширована из торрента
            assert b.cover_cache_path, f"книга {i}: cover_cache_path пуст"
        # статусы агрегированы
        assert all(b.status == "announced" for b in books), \
            f"статусы книг: {[b.status for b in books]}"
        assert rel.status == "announced", f"rel.status={rel.status}"

        series = db.query(Series).all()
        assert len(series) == 1, f"серий создано: {len(series)}"
        assert series[0].title == "Ночной Дозор", f"серия={series[0].title!r}"
        print("ДБ-слой: OK (2 книги, локальные ordinals, серия, обложки томов)")
    finally:
        db.close()

    # --- 5. сервисный слой --------------------------------------------------
    db = SessionLocal()
    try:
        releases, total = service.list_releases(db)
        assert total == 1 and len(releases) == 1, f"releases={total}"
        r = releases[0]
        assert r.content_type == "series" and len(r.books) == 2

        # фильтр по серии
        releases_s, total_s = service.list_releases(db, series_id=series[0].id)
        assert total_s == 1, f"по серии найдено {total_s}"

        detail = service.get_release(db, release_id)
        assert detail is not None and len(detail.books) == 2
        assert len(detail.books[0].chapters) == 2

        books_all, books_total = service.list_books(db)
        assert books_total == 2 and len(books_all) == 2

        book = service.get_book(db, books_all[0].id)
        assert book is not None and len(book.chapters) == 2
        assert book.series is not None and book.series_order is not None

        with patch("app.torrents.manager.get_torrent", return_value=None):
            st = service.get_download_status(db, books_all[0].id)
            assert st is not None
            assert st["release_id"] == release_id, f"release_id={st['release_id']}"
            assert st["chapters_total"] == 2 and st["book_id"] == books_all[0].id
        print("Сервисный слой: OK (list/get, фильтр по серии, статус с release_id)")
    finally:
        db.close()

    # --- 6. HTTP-слой -------------------------------------------------------
    app = create_app()
    client = TestClient(app)  # без `with` — lifespan (скрейпинг/демоны) не стартует

    # get_download_status обращается к торрент-клиенту — замокаем
    with patch("app.torrents.manager.get_torrent", return_value=None):
        rp = client.get("/releases")
        assert rp.status_code == 200, rp.status_code
        body = rp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["content_type"] == "series"
        assert item["books_count"] == 2
        assert item["title"] == title
        assert item["author"] == "Сергей Лукьяненко"
        assert item["narrator"] == "Иван Иванов"
        assert item["rubrics"] == ["Фантастика"]

        rp = client.get(f"/releases/{release_id}")
        assert rp.status_code == 200, rp.status_code
        detail_body = rp.json()
        assert len(detail_body["books"]) == 2
        b0 = detail_body["books"][0]
        assert b0["ordinal"] == 1 and len(b0["chapters"]) == 2
        assert b0["series"]["title"] == "Ночной Дозор"
        assert b0["series"]["order"] == 1
        assert b0["folder"] == folders[0]
        assert b0["cover_url"].endswith(f"/books/{b0['id']}/cover")

        rp = client.get("/books")
        assert rp.status_code == 200, rp.status_code
        books_body = rp.json()
        assert books_body["total"] == 2
        bk = books_body["items"][0]
        assert bk["ordinal"] in (1, 2)
        assert bk["year"] is None  # карточка без года
        assert bk["series"]["title"] == "Ночной Дозор"

        bk_id = bk["id"]
        rp = client.get(f"/books/{bk_id}")
        assert rp.status_code == 200, rp.status_code
        bkd = rp.json()
        assert bkd["id"] == bk_id
        assert bkd["description"] == "Два первых тома цикла"

        # обложки: карточки и книги — placeholder/кэш, всегда 200 + картинка
        rp = client.get(f"/releases/{release_id}/cover")
        assert rp.status_code == 200, rp.status_code
        assert rp.headers["content-type"].startswith(("image/",))

        rp = client.get(f"/books/{bk_id}/cover")
        assert rp.status_code == 200, rp.status_code
        assert rp.headers["content-type"].startswith(("image/",))

        # статус загрузки книги
        rp = client.get(f"/books/{bk_id}/status")
        assert rp.status_code == 200, rp.status_code
        st_body = rp.json()
        assert st_body["release_id"] == release_id, st_body
        assert st_body["book_id"] == bk_id
        assert st_body["chapters_total"] == 2
        print("HTTP-слой: OK (releases/books/cover/status)")

    # =====================================================================
    # 7. ВРЕМЕННАЯ ОБЛОЖКА КНИГИ (в торренте нет картинок)
    # =====================================================================
    temp_torrent = "Дозоры - полное собрание (2005)"
    temp_title = "Дозоры - полное собрание"
    db = SessionLocal()
    try:
        trelease = Release(
            torrent_hash="e" * 40,
            magnet="magnet:?xt=urn:btih:" + "e" * 40,
            source_url="https://rutor.info/torrent/99999",
            save_path=str(settings.downloads_dir),
            title=temp_title,
            title_norm=norm(temp_title),
            title_raw="Дозоры - полное собрание (2005)",
            description="полное собрание дозоров",
            language="ru",
            size_bytes=8 * 1024,
            content_type="single",
        )
        db.add(trelease)
        db.flush()
        db.add(ReleasePerson(
            release_id=trelease.id,
            person_id=author.id,
            role="author",
        ))
        temp_release_id = trelease.id
        db.commit()
    finally:
        db.close()

    tfolders = ["Дозор. Том 1", "Дозор. Том 2"]
    # метаданные торрента без картинок; физических файлов нет (не качается)
    tfiles = make_files(temp_torrent, tfolders, files_per_book=2,
                        include_cover=False)
    created = ingest_release(temp_release_id, tfiles, temp_torrent, cover_svc)
    assert created == 4, f"temp-раздача: глав {created}"

    db = SessionLocal()
    try:
        trel = db.get(Release, temp_release_id)
        assert trel.content_type == "series", trel.content_type
        tbooks = list(trel.books)
        assert len(tbooks) == 2, f"temp-раздача: книг {len(tbooks)}"
        for b in tbooks:
            assert b.cover_is_temp, f"книга {b.id}: cover_is_temp должен быть True"
            assert b.cover_cache_path is None, f"книга {b.id}: своя обложка не должна быть"
            assert b.cover_path_hint is None, \
                f"книга {b.id}: hint нет (в торренте картинок не было)"
        print("Сценарий temp-обложки: ingest -> cover_is_temp=True, hint=None")
    finally:
        db.close()

    # обложка карточки-раздачи нужна, чтобы temp-обложка книги её отдавала
    cover_jpg = downloads / "_temp_release_cover.jpg"
    cover_jpg.write_bytes(_JPEG)
    db = SessionLocal()
    try:
        trel = db.get(Release, temp_release_id)
        assert cover_svc.cache_from_torrent(trel, cover_jpg), \
            "обложка раздачи не закэширована"
        db.commit()
    finally:
        db.close()

    # download.start() — подтверждает временную обложку
    db = SessionLocal()
    try:
        trel = db.get(Release, temp_release_id)
        book0 = trel.books[0]
        book0_id = book0.id
        with patch("app.torrents.manager.get_torrent", return_value=None), \
             patch("app.torrents.manager.start_download",
                   new=AsyncMock(return_value=777)):
            res = asyncio.run(download.start(db, book0_id))
        assert res.success, f"download.start: {res}"
        db.commit()
        assert book0.cover_is_temp, "download.start сбросил временную обложку"
        assert book0.cover_cache_path is None
        print("Сценарий temp-обложки: download.start() сохраняет cover_is_temp")
    finally:
        db.close()

    # HTTP: /books/{id}/cover отдаёт обложку раздачи с признаком temp
    rp = client.get(f"/books/{book0_id}/cover")
    assert rp.status_code == 200, rp.status_code
    assert rp.headers.get("x-cover-temp") == "true", \
        f"x-cover-temp={rp.headers.get('x-cover-temp')!r}"
    assert rp.headers.get("cache-control") == "no-cache", \
        f"cache-control={rp.headers.get('cache-control')!r}"
    print("Сценарий temp-обложки: HTTP X-Cover-Temp: true, Cache-Control: no-cache")

    # онлайн-поиск по названию книги → своя обложка, temp снят
    db = SessionLocal()
    try:
        trel = db.get(Release, temp_release_id)
        book0 = trel.books[0]
        with patch("app.catalog.cover_search.search_cover",
                   new=_search_ok), \
             patch.object(cover_svc._client, "get",
                          new=AsyncMock(return_value=_FakeResp())):
            ok = asyncio.run(try_search_book_cover(cover_svc, book0, trel))
        assert ok, "онлайн-поиск обложки не удался"
        db.commit()
        assert book0.cover_cache_path, "после поиска нет cover_cache_path"
        assert not book0.cover_is_temp, "cover_is_temp должен стать False"
        assert book0.cover_origin == "online", f"origin={book0.cover_origin}"
        print("Сценарий temp-обложки: онлайн-поиск -> своя обложка, temp снят")
    finally:
        db.close()

    # HTTP: теперь своя обложка — без признака temp, с длинным кэшем
    rp = client.get(f"/books/{book0_id}/cover")
    assert rp.status_code == 200, rp.status_code
    assert rp.headers.get("x-cover-temp") != "true", \
        f"x-cover-temp={rp.headers.get('x-cover-temp')!r}"
    assert rp.headers.get("cache-control") == "public, max-age=604800", \
        f"cache-control={rp.headers.get('cache-control')!r}"
    print("Сценарий temp-обложки: HTTP своя обложка (temp снят, долгий кэш)")

    # =====================================================================
    # 8. ДОКАЧКА ОБЛОЖКИ ТОМА ПО cover_path_hint
    # =====================================================================
    hint_torrent = "Метро 2033 - сборник (2006)"
    hint_title = "Метро 2033 - сборник"
    db = SessionLocal()
    try:
        hrelease = Release(
            torrent_hash="d" * 40,
            magnet="magnet:?xt=urn:btih:" + "d" * 40,
            source_url="https://rutor.info/torrent/88888",
            save_path=str(settings.downloads_dir),
            title=hint_title,
            title_norm=norm(hint_title),
            title_raw="Метро 2033 - сборник (2006)",
            description="сборник метро",
            language="ru",
            size_bytes=9 * 1024,
            content_type="single",
        )
        db.add(hrelease)
        db.flush()
        db.add(ReleasePerson(
            release_id=hrelease.id,
            person_id=author.id,
            role="author",
        ))
        hint_release_id = hrelease.id
        db.commit()
    finally:
        db.close()

    hfolders = ["Метро 2033. Том 1", "Метро 2033. Том 2"]
    # картинки заявлены в торренте, но файлов на диске ещё нет — как при
    # старте загрузки: структура показывает cover.jpg, физически отсутствует
    hfiles = make_files(hint_torrent, hfolders, files_per_book=2,
                        include_cover=True)
    created = ingest_release(hint_release_id, hfiles, hint_torrent, cover_svc)
    assert created == 4, f"hint-раздача: глав {created}"

    db = SessionLocal()
    try:
        hrel = db.get(Release, hint_release_id)
        hbook0 = hrel.books[0]
        hbook0_id = hbook0.id
        assert hbook0.cover_path_hint, "cover_path_hint не сохранён при ingest"
        assert hbook0.cover_is_temp, "cover_is_temp должен быть True (файла нет)"
        assert hbook0.cover_cache_path is None
        db.commit()
    finally:
        db.close()

    # файл докачался на диск → докачка/копирование обложки, temp снят
    hint_dir = downloads / hint_torrent / hfolders[0]
    hint_dir.mkdir(parents=True, exist_ok=True)
    (hint_dir / "cover.jpg").write_bytes(_JPEG)

    db = SessionLocal()
    try:
        hrel = db.get(Release, hint_release_id)
        hbook0 = hrel.books[0]
        changed = poller._try_copy_book_cover(hbook0, cover_svc)
        assert changed, "докачка обложки тома не сработала"
        db.commit()
        assert hbook0.cover_cache_path, "нет cover_cache_path после докачки"
        assert not hbook0.cover_is_temp, "cover_is_temp должен стать False"
        print("Сценарий hint: файл докачался -> обложка скопирована, temp снят")
    finally:
        db.close()

    rp = client.get(f"/books/{hbook0_id}/cover")
    assert rp.status_code == 200, rp.status_code
    assert rp.headers.get("x-cover-temp") != "true", \
        f"x-cover-temp={rp.headers.get('x-cover-temp')!r}"
    print("Сценарий hint: HTTP своя обложка (из файла тома)")

    # --- итог ---------------------------------------------------------------
    print("\n=== ПРОХОДИТ: сквозной сценарий многокнижной раздачи OK ===")
    print("   + temp-обложка (X-Cover-Temp, онлайн-поиск, снятие флага)")
    print("   + докачка обложки тома по cover_path_hint")
    print(f"temp DATA_DIR: {_TMP}")


if __name__ == "__main__":
    main()