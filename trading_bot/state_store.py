"""Lightweight ops state (pause, live confirm, scan metrics, paper confirm gates)."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# Mirrored from production tg_i2 OpsControlState
RESET_PAPER_CONFIRM_TTL_SECONDS = 60.0
WIPE_PAPER_CONFIRM_TTL_SECONDS = 60.0

logger = logging.getLogger(__name__)

# Wall-clock CB cooldown file (survives process restart; monotonic alone does not).
DEFAULT_CB_STATE_PATH = Path("data/cb_auto_resume.json")
# Post-CB PHD WEAK soft-gate bypass (survives restart; cleared on next CB arm).
DEFAULT_PHD_BYPASS_PATH = Path("data/cb_phd_bypass.json")


@dataclass
class OpsState:
    paused: bool = False
    live_confirmed: bool = False
    kill_requested: bool = False
    cb_enabled: bool = True  # master switch (losses always counted)
    cb_active: bool = False  # True while CB pause is in effect
    cb_auto_resume_armed: bool = False
    cb_auto_resume_at: float = 0.0  # monotonic deadline
    cb_auto_resume_at_wall: float = 0.0  # epoch deadline (restart-safe)
    cb_win_since_trip: bool = False
    CB_AUTO_RESUME_COOLDOWN_SEC: float = 45 * 60
    cb_state_path: Optional[Path] = None
    phd_bypass_path: Optional[Path] = None
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

    def _cb_path(self) -> Path:
        return Path(self.cb_state_path) if self.cb_state_path else DEFAULT_CB_STATE_PATH

    def _phd_bypass_path(self) -> Path:
        return Path(self.phd_bypass_path) if self.phd_bypass_path else DEFAULT_PHD_BYPASS_PATH

    def _persist_phd_weak_bypass(self) -> None:
        """Write phd_weak_bypass_after_cb so WEAK soft-gate stays off across restarts."""
        path = self._phd_bypass_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            active = bool(self.extra.get("phd_weak_bypass_after_cb"))
            if not active:
                if path.exists():
                    path.unlink()
                return
            payload = {
                "phd_weak_bypass_after_cb": True,
                "updated_at": time.time(),
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("phd bypass persist failed: %s", exc)

    def _clear_phd_weak_bypass(self) -> None:
        self.extra.pop("phd_weak_bypass_after_cb", None)
        path = self._phd_bypass_path()
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("phd bypass clear failed: %s", exc)

    def set_phd_weak_bypass_after_cb(self, value: bool = True) -> None:
        """Enable/disable post-CB WEAK soft-gate bypass and persist to disk."""
        if value:
            self.extra["phd_weak_bypass_after_cb"] = True
            self._persist_phd_weak_bypass()
        else:
            self._clear_phd_weak_bypass()

    def restore_phd_weak_bypass_from_disk(self) -> bool:
        """Reload phd_weak_bypass_after_cb after process restart. Returns True if restored."""
        path = self._phd_bypass_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("phd bypass load failed: %s", exc)
            return False
        if not bool(data.get("phd_weak_bypass_after_cb")):
            return False
        self.extra["phd_weak_bypass_after_cb"] = True
        logger.info("restored phd_weak_bypass_after_cb from disk")
        return True

    def _persist_cb_state(self) -> None:
        path = self._cb_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "paused": bool(self.paused),
                "cb_active": bool(self.cb_active),
                "cb_auto_resume_armed": bool(self.cb_auto_resume_armed),
                "cb_auto_resume_at_wall": float(self.cb_auto_resume_at_wall or 0.0),
                "cb_win_since_trip": bool(self.cb_win_since_trip),
                "updated_at": time.time(),
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("cb state persist failed: %s", exc)

    def _clear_persisted_cb_state(self) -> None:
        path = self._cb_path()
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cb state clear failed: %s", exc)

    def restore_cb_state_from_disk(self) -> bool:
        """Reload CB pause + wall deadline after process restart. Returns True if restored."""
        path = self._cb_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cb state load failed: %s", exc)
            return False
        armed = bool(data.get("cb_auto_resume_armed"))
        active = bool(data.get("cb_active"))
        wall = float(data.get("cb_auto_resume_at_wall") or 0.0)
        paused = bool(data.get("paused"))
        if not (armed or active or paused):
            return False
        self.paused = True if (armed or active) else paused
        self.cb_active = active or armed
        self.cb_auto_resume_armed = armed or (wall > 0 and self.paused)
        self.cb_auto_resume_at_wall = wall
        # Rebuild monotonic deadline from remaining wall time.
        rem = max(0.0, wall - time.time()) if wall > 0 else 0.0
        self.cb_auto_resume_at = time.monotonic() + rem if rem > 0 else 0.0
        self.cb_win_since_trip = bool(data.get("cb_win_since_trip"))
        logger.info(
            "restored CB auto-resume from disk (remaining=%.0fs active=%s)",
            rem,
            self.cb_active,
        )
        return True

    def set_pause(self, value: bool) -> None:
        self.paused = bool(value)
        if not self.paused:
            self.clear_cb_auto_resume()

    def arm_cb_auto_resume(self, cooldown_sec: float | None = None) -> int:
        cd = float(cooldown_sec if cooldown_sec is not None else self.CB_AUTO_RESUME_COOLDOWN_SEC)
        cd = max(60.0, cd)
        self.cb_auto_resume_armed = True
        self.cb_auto_resume_at = time.monotonic() + cd
        self.cb_auto_resume_at_wall = time.time() + cd
        self.cb_win_since_trip = False
        self.cb_active = True
        self.paused = True
        # Next CB trip: drop any post-resume PHD WEAK soft-gate bypass (memory + disk).
        self._clear_phd_weak_bypass()
        self._persist_cb_state()
        return int(round(cd / 60.0))

    def clear_cb_auto_resume(self) -> None:
        self.cb_auto_resume_armed = False
        self.cb_auto_resume_at = 0.0
        self.cb_auto_resume_at_wall = 0.0
        self.cb_win_since_trip = False
        self.cb_active = False
        self._clear_persisted_cb_state()

    def cb_pause_active(self) -> bool:
        """True while a circuit-breaker pause (trip) is in effect."""
        return bool(self.paused) and (
            bool(self.cb_active) or bool(self.cb_auto_resume_armed)
        )

    def cb_auto_resume_remaining_seconds(self) -> float:
        """Seconds until gated CB auto-resume may fire (0 if not armed/paused)."""
        if not self.paused:
            return 0.0
        if not (self.cb_auto_resume_armed or self.cb_active):
            return 0.0
        # Prefer wall clock (restart-safe once restored).
        if float(self.cb_auto_resume_at_wall or 0.0) > 0:
            rem = float(self.cb_auto_resume_at_wall) - time.time()
            return max(0.0, rem)
        if self.cb_auto_resume_armed and float(self.cb_auto_resume_at or 0.0) > 0:
            rem = float(self.cb_auto_resume_at) - time.monotonic()
            return max(0.0, rem)
        return 0.0

    def cb_auto_resume_ready(self, *, regime_bull_ok: bool) -> bool:
        """True when CB cooldown has expired while still armed+paused.

        Timer expiry IS the decision — callers may pass regime_bull_ok for
        logging/API compat; win/regime are not required to resume.
        """
        if not self.cb_auto_resume_armed or not self.paused:
            return False
        if self.cb_auto_resume_remaining_seconds() > 0.0:
            return False
        # Keep param referenced so callers/linters stay happy.
        _ = bool(regime_bull_ok)
        return True


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
