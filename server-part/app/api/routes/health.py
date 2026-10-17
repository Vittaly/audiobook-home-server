import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.torrents.daemon import resolve_mode

router = APIRouter()

_STARTED_AT = time.time()


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    # Режим торрент-клиента — единый источник истины (TORRENT_BACKEND)
    try:
        mode = resolve_mode()
    except ValueError:
        mode = settings.TORRENT_BACKEND.strip().lower()

    # Статус автозапуска проставляется в lifespan (app.state.torrent_client_ok)
    state = request.app.state
    client_ok = bool(getattr(state, "torrent_client_ok", False))

    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "uptime_sec": int(time.time() - _STARTED_AT),
        "mode": mode,
        "torrent_client": "ready" if client_ok else "unavailable",
    }