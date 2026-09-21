"""Exponential backoff helpers for Kraken / Telegram / xAI."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def with_exponential_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    label: str = "call",
) -> T:
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = min(max_delay, base_delay * (2 ** attempt))
            delay *= 0.5 + random.random()  # jitter
            logger.warning("%s failed (attempt %s): %s; backoff %.1fs", label, attempt + 1, exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
