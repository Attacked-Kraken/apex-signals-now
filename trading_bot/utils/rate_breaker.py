"""Consecutive rate-limit circuit breaker with cooldown."""
from __future__ import annotations

import time
from typing import Optional


class RateLimitBreaker:
    def __init__(
        self,
        *,
        trip_after: int = 2,
        cooldown_seconds: float = 120.0,
        max_cooldown: float = 300.0,
    ) -> None:
        self.trip_after = max(1, int(trip_after))
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_cooldown = float(max_cooldown)
        self._consecutive = 0
        self._cooldown_until = 0.0
        self._reason = ""
        self._on_trip = None
        self.ban_risk = None

    def record_rate_limit(self, retry_after: Optional[float] = None) -> None:
        if self.ban_risk is not None:
            self.ban_risk.note_429("api")
        self._consecutive += 1
        if self._consecutive >= self.trip_after:
            ra = float(retry_after or 0.0)
            cd = min(self.max_cooldown, max(self.cooldown_seconds, ra))
            self._cooldown_until = time.time() + cd
            self._reason = (
                f"rate-limit breaker tripped after {self._consecutive} hits; "
                f"cooldown {cd:.0f}s"
            )
            if self._on_trip is not None:
                try:
                    self._on_trip()
                except Exception:  # noqa: BLE001
                    pass

    def record_success(self) -> None:
        if self.ban_risk is not None:
            self.ban_risk.note_success("api")
        self._consecutive = 0

    def cooling_down(self) -> bool:
        return time.time() < float(self._cooldown_until or 0.0)

    def remaining_seconds(self) -> float:
        rem = float(self._cooldown_until or 0.0) - time.time()
        return max(0.0, rem)

    def trip_reason(self) -> str:
        if self.cooling_down():
            return self._reason or "rate-limit cooldown"
        return ""

    def set_on_trip(self, cb) -> None:
        self._on_trip = cb
