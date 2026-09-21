"""Lightweight ops state (pause, live confirm, scan metrics, paper confirm gates)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Mirrored from production tg_i2 OpsControlState
RESET_PAPER_CONFIRM_TTL_SECONDS = 60.0
WIPE_PAPER_CONFIRM_TTL_SECONDS = 60.0


@dataclass
class OpsState:
    paused: bool = False
    live_confirmed: bool = False
    kill_requested: bool = False
    cb_enabled: bool = True  # master switch (losses always counted)
    cb_active: bool = False  # True while CB pause is in effect
    cb_auto_resume_armed: bool = False
    cb_auto_resume_at: float = 0.0
    cb_win_since_trip: bool = False
    CB_AUTO_RESUME_COOLDOWN_SEC: float = 45 * 60
    last_scan_ms: Optional[float] = None
    last_scan_n: Optional[int] = None
    last_tick_ts: float = field(default_factory=time.time)
    rate_limit_cooldown_until: float = 0.0  # epoch seconds (time.time())
    focus_symbol: str = ""
    focus_score: float = 0.0
    focus_blocked: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # Paper confirm gates (~60s)
    _pending_reset_paper_until: float = 0.0
    _pending_reset_paper_cash: Optional[float] = None
    _pending_reset_paper_explicit: bool = False
    _pending_wipe_paper_until: float = 0.0
    _pending_wipe_paper_cash: Optional[float] = None
    _pending_wipe_paper_explicit: bool = False

    def set_pause(self, value: bool) -> None:
        self.paused = bool(value)
        if not self.paused:
            self.clear_cb_auto_resume()

    def arm_cb_auto_resume(self, cooldown_sec: float | None = None) -> int:
        cd = float(cooldown_sec if cooldown_sec is not None else self.CB_AUTO_RESUME_COOLDOWN_SEC)
        self.cb_auto_resume_armed = True
        self.cb_auto_resume_at = time.monotonic() + max(60.0, cd)
        self.cb_win_since_trip = False
        self.cb_active = True
        return int(round(cd / 60.0))

    def clear_cb_auto_resume(self) -> None:
        self.cb_auto_resume_armed = False
        self.cb_auto_resume_at = 0.0
        self.cb_win_since_trip = False
        self.cb_active = False

    def cb_auto_resume_ready(self, *, regime_bull_ok: bool) -> bool:
        if not self.cb_auto_resume_armed or not self.paused:
            return False
        if time.monotonic() < float(self.cb_auto_resume_at or 0.0):
            return False
        return bool(self.cb_win_since_trip) or bool(regime_bull_ok)


    def touch_tick(self) -> None:
        self.last_tick_ts = time.time()

    def tick_age_seconds(self) -> float:
        return max(0.0, time.time() - self.last_tick_ts)

    def tick_age(self) -> str:
        age = self.tick_age_seconds()
        if age < 60:
            return f"{age:.0f}s"
        return f"{age/60:.1f}m"

    def arm_reset_paper_confirm(
        self,
        cash: float,
        *,
        explicit: bool = False,
        ttl: float = RESET_PAPER_CONFIRM_TTL_SECONDS,
    ) -> float:
        ttl_f = float(ttl)
        self._pending_reset_paper_until = time.monotonic() + ttl_f
        self._pending_reset_paper_cash = float(cash)
        self._pending_reset_paper_explicit = bool(explicit)
        return ttl_f

    def clear_reset_paper_confirm(self) -> None:
        self._pending_reset_paper_until = 0.0
        self._pending_reset_paper_cash = None
        self._pending_reset_paper_explicit = False

    def has_pending_reset_paper_confirm(self, now: Optional[float] = None) -> bool:
        deadline = float(self._pending_reset_paper_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self.clear_reset_paper_confirm()
            return False
        return True

    def pending_reset_paper_cash(self) -> Optional[float]:
        if not self.has_pending_reset_paper_confirm():
            return None
        return self._pending_reset_paper_cash

    def pending_reset_paper_explicit(self) -> bool:
        if not self.has_pending_reset_paper_confirm():
            return False
        return bool(self._pending_reset_paper_explicit)

    def arm_wipe_paper_confirm(
        self,
        cash: float,
        *,
        explicit: bool = False,
        ttl: float = WIPE_PAPER_CONFIRM_TTL_SECONDS,
    ) -> float:
        ttl_f = float(ttl)
        self._pending_wipe_paper_until = time.monotonic() + ttl_f
        self._pending_wipe_paper_cash = float(cash)
        self._pending_wipe_paper_explicit = bool(explicit)
        return ttl_f

    def clear_wipe_paper_confirm(self) -> None:
        self._pending_wipe_paper_until = 0.0
        self._pending_wipe_paper_cash = None
        self._pending_wipe_paper_explicit = False

    def has_pending_wipe_paper_confirm(self, now: Optional[float] = None) -> bool:
        deadline = float(self._pending_wipe_paper_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self.clear_wipe_paper_confirm()
            return False
        return True

    def pending_wipe_paper_cash(self) -> Optional[float]:
        if not self.has_pending_wipe_paper_confirm():
            return None
        return self._pending_wipe_paper_cash

    def pending_wipe_paper_explicit(self) -> bool:
        if not self.has_pending_wipe_paper_confirm():
            return False
        return bool(self._pending_wipe_paper_explicit)
