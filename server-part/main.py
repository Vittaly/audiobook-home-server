import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import uvicorn

from app.api.server import create_app
from app.config import settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)

_uvicorn_server: uvicorn.Server | None = None


async def _serve(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except asyncio.CancelledError:
        log.info(">>> main: serve отменён")
        raise


async def main() -> None:
    global _uvicorn_server

    setup_logging()
    log.info("=== audiobook-server запускается ===")
    log.info(">>> main: __file__=%s", __file__)
    log.info(">>> main: data_dir=%s", settings.data_dir)

    app = create_app()
    config = uvicorn.Config(
        app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    _uvicorn_server = server

    stop_event = asyncio.Event()

    def request_stop(sig_name: str) -> None:
        log.warning(">>> main: получен %s, останавливаем uvicorn", sig_name)
        server.should_exit = True
        stop_event.set()

    loop = asyncio.get_running_loop()

    if sys.platform == "win32":
        def _win_handler(signum, frame):
            loop.call_soon_threadsafe(request_stop, f"signal {signum}")
        signal.signal(signal.SIGINT, _win_handler)
        signal.signal(signal.SIGTERM, _win_handler)
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop, sig.name)

    log.info(">>> main: запускаем API на http://%s:%d",
             settings.API_HOST, settings.API_PORT)

    t0 = datetime.now(timezone.utc)

    try:
        await server.serve()
    except asyncio.CancelledError:
        log.info(">>> main: serve отменён")
    finally:
        dt = (datetime.now(timezone.utc) - t0).total_seconds()
        log.info("=== audiobook-server остановлен (%.1f сек) ===", dt)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(">>> main: KeyboardInterrupt в asyncio.run")