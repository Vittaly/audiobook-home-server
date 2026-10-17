from app.db.session import SessionLocal
from app.db.models import Release
from sqlalchemy import select

db = SessionLocal()
rows = db.execute(select(Release).limit(10)).scalars().all()
for r in rows:
    print(f'hash={r.torrent_hash[:8]}  source_url={r.source_url!r}')