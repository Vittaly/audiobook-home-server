"""
Middleware для логирования HTTP-запросов.

Пишет каждое обращение в общий лог:
  время | уровень | app.api.access | METHOD path → status за Xms

Уровни:
  INFO  — 2xx, 3xx
  WARNING — 4xx
  ERROR — 5xx

Пропускает шумные пути (/health, /docs, /openapi.json, /redoc).
"""

import logging
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("app.api.access")

# не логируем эти пути — слишком часто или бесполезно
_SKIP_PATHS = {
    "/health",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/favicon.ico",
}


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Логирует каждый HTTP-запрос с длительностью обработки."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # пропускаем служебные пути
        if path in _SKIP_PATHS or path.startswith("/static/"):
            return await call_next(request)

        method = request.method
        client = request.client.host if request.client else "?"
        user_agent = request.headers.get("user-agent", "")

        # Range и If-None-Match — важны для стриминга
        range_header = request.headers.get("range", "")
        etag_header = request.headers.get("if-none-match", "")

        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            dt_ms = (time.perf_counter() - t0) * 1000
            log.exception("!!! %s %s → EXCEPTION за %.1f ms (client=%s)",
                         method, path, dt_ms, client)
            raise

        dt_ms = (time.perf_counter() - t0) * 1000
        status = response.status_code

        # формируем строку запроса
        extras = []
        if range_header:
            extras.append(f"range={range_header}")
        if etag_header:
            extras.append(f"if-none-match={etag_header[:24]}…")
        extras_str = " " + " ".join(extras) if extras else ""

        line = "%s %s → %d за %.1f ms (client=%s)%s"
        args = (method, path, status, dt_ms, client, extras_str)

        if status >= 500:
            log.error(line, *args)
        elif status >= 400:
            log.warning(line, *args)
        else:
            log.info(line, *args)

        return response