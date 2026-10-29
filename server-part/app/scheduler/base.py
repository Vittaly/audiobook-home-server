"""
Базовый класс для фоновых задач (Job).

Каждая задача наследует Job и реализует run().
execute() — обёртка с логированием, обработкой CancelledError и Exception.
Можно передавать в APScheduler как callable (execute).
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class Job(ABC):
    """Базовый класс фоновой задачи."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Короткое имя задачи (для логов)."""
        ...

    @abstractmethod
    async def run(self) -> int | None:
        """
        Основная логика задачи.

        Returns:
            Количество обработанных элементов или None.
        """
        ...

    async def execute(self) -> int | None:
        """
        Обёртка с логированием старт/финиш/ошибка/отмена.

        APScheduler получает этот метод как callable.
        """
        t0 = datetime.now(timezone.utc)
        log.info(">>> task %s: СТАРТ", self.name)
        try:
            result = await self.run()
            dt = (datetime.now(timezone.utc) - t0).total_seconds()
            log.info(">>> task %s: ГОТОВО (%s) за %.1f сек",
                     self.name, result, dt)
            return result
        except asyncio.CancelledError:
            dt = (datetime.now(timezone.utc) - t0).total_seconds()
            log.info(">>> task %s: ОТМЕНЕНА за %.1f сек", self.name, dt)
            return None
        except Exception:
            dt = (datetime.now(timezone.utc) - t0).total_seconds()
            log.exception(">>> task %s: ОШИБКА за %.1f сек", self.name, dt)
            return None