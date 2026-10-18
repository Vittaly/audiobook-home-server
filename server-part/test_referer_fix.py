"""
Регрессия на баг HTTP 500 при старте загрузки раздачи.

Причина: source_url раздачи с rutor мог содержать кириллицу/«ёлочки»
(например « = \xab). Этот URL подставлялся в заголовок Referer, а httpx
кодирует значения заголовков как ASCII → UnicodeEncodeError → 500.

Проверяем:
  1. fetch_torrent_bytes не бросает исключение на кириллическом source_url
     и передаёт в httpx ASCII-совместимый Referer.
  2. quote() в парсере rutor делает путь ASCII-совместимым.

Запуск:  python test_referer_fix.py
"""

import asyncio
from unittest import mock

from app.scraper.rutor import _parse_row  # noqa: F401  (импорт для дымовой проверки)
from app.torrents import manager

# source_url с «ёлочками» (\xab, \xbb) — именно такие приходят с rutor.
CYRILLIC_SOURCE_URL = (
    "https://rutor.info/torrent/266/viktor-«test»-kniga-2020"
)


def _check_ascii(value: str, label: str) -> None:
    try:
        value.encode("ascii")
    except UnicodeEncodeError as e:
        raise AssertionError(
            f"{label} не ASCII-совместим: {value!r} ({e})"
        )


def test_fetch_torrent_bytes_referer_is_ascii() -> None:
    """Referer, уходящий в httpx, должен быть ASCII, а вызов — без исключений."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        # bencode-подобное тело достаточной длины
        content = b"d8:announce30:http://tracker.example/announceee" + b"0" * 60

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            captured["url"] = url
            return FakeResponse()

    with mock.patch.object(manager.httpx, "AsyncClient", FakeClient):
        # Не должно бросить UnicodeEncodeError.
        result = asyncio.run(
            manager.fetch_torrent_bytes(CYRILLIC_SOURCE_URL)
        )

    headers = captured.get("headers", {})
    referer = headers.get("Referer", "")
    _check_ascii(referer, "Referer")

    # Referer построен из torrent_id, а не из сырого URL.
    assert referer == f"{manager.settings.RUTOR_BASE_URL}/torrent/266", referer
    # Сам запрос идёт на d.rutor.info/download/<id>.
    assert captured["url"].endswith("/download/266"), captured["url"]
    # .torrent получен.
    assert isinstance(result, (bytes, bytearray)), result

    print("OK  fetch_torrent_bytes: Referer ASCII-совместим, без исключений")


def test_source_url_would_have_failed_before() -> None:
    """Демонстрация: сырой URL действительно ломает ASCII-кодирование."""
    try:
        CYRILLIC_SOURCE_URL.encode("ascii")
    except UnicodeEncodeError:
        print("OK  сырой source_url действительно не ASCII (воспроизведён баг)")
        return
    raise AssertionError("ожидался UnicodeEncodeError для сырого source_url")


def test_quote_encodes_cyrillic() -> None:
    from urllib.parse import quote
    encoded = quote(
        "/torrent/266/viktor-«test»", safe="/:@!$&'()*+,;=~-._"
    )
    _check_ascii(encoded, "quoted href")
    assert encoded == "/torrent/266/viktor-%C2%ABtest%C2%BB", encoded
    print("OK  quote(): кириллица/«ёлочки» -> percent-encoding")


if __name__ == "__main__":
    test_source_url_would_have_failed_before()
    test_fetch_torrent_bytes_referer_is_ascii()
    test_quote_encodes_cyrillic()
    print("\nВсе регрессионные проверки пройдены.")
