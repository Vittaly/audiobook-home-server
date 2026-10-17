"""
Работа с CrawlState: загрузка, сохранение, поиск по источнику.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CrawlState, Source

log = logging.getLogger(__name__)


class CrawlStateStore:
    """Инкапсулирует доступ к CrawlState."""

    def __init__(self, db: Session):
        self.db = db

    def load(self, source_name: str, base_url: str,
             category: str) -> CrawlState:
        source = self._ensure_source(source_name, base_url)
        state = self.db.execute(
            select(CrawlState).where(
                CrawlState.source_id == source.id,
                CrawlState.category == category,
            )
        ).scalar_one_or_none()
        if state is None:
            state = CrawlState(source_id=source.id, category=category)
            self.db.add(state)
            self.db.flush()
        return state

    def save_progress(self, state: CrawlState) -> None:
        """Сохраняет позицию обхода. Вызывается из on_progress."""
        state.last_run_at = datetime.utcnow()
        self.db.commit()

    def finish(self, state: CrawlState) -> None:
        """Обход завершён полностью — сохраняем финальное состояние."""
        state.last_run_at = datetime.utcnow()
        self.db.commit()

    def _ensure_source(self, name: str, base_url: str) -> Source:
        source = self.db.execute(
            select(Source).where(Source.name == name)
        ).scalar_one_or_none()
        if source is None:
            source = Source(name=name, base_url=base_url)
            self.db.add(source)
            self.db.flush()
        return source