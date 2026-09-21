"""Sliding-window cap for live broker order calls (AddOrder/Cancel)."""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional


class OrderRateLimiter:
    def __init__(self, *, max_per_minute: int = 6) -> None:
        self.max_per_minute = max(1, int(max_per_minute))
        self._hits: Deque[float] = deque()

    def _prune(self) -> None:
        cutoff = time.time() - 60.0
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def allow(self) -> bool:
        self._prune()
        return len(self._hits) < self.max_per_minute

    def remaining(self) -> int:
        self._prune()
        return max(0, self.max_per_minute - len(self._hits))

    def record(self) -> None:
        self._prune()
        self._hits.append(time.time())

    def block_reason(self) -> str:
        self._prune()
        return (
            f"⛔ LIVE order rate cap: {len(self._hits)}/{self.max_per_minute} "
            f"orders in the last 60s"
        )
