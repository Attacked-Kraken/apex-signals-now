"""Risk / sizing / exposure — MAX_TOTAL_EXPOSURE_USD is fixed (never equity-scaled)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from trading_bot.models import RiskVerdict

if TYPE_CHECKING:
    from trading_bot.config import Settings

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, settings: "Settings"):
        self.settings = settings
        self._consecutive_losses = 0
        self._pause_cb: Optional[Callable[[bool], None]] = None
        self._notify_cb: Optional[Callable[[str], None]] = None

    def set_ops(self, pause_cb: Callable[[bool], None], notify_cb: Optional[Callable[[str], None]] = None) -> None:
        self._pause_cb = pause_cb
        self._notify_cb = notify_cb

    def set_consecutive_losses(self, n: int) -> None:
        self._consecutive_losses = max(0, int(n))

    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def record_trade_result(self, won: bool, *, paused: bool = False) -> None:
        if won:
            self._consecutive_losses = 0
            return
        self._consecutive_losses += 1
        # Losses always counted; auto-pause only when circuit breaker master switch is ON.
        cb_on = bool(getattr(self.settings, "circuit_breaker_enabled", True))
        limit = 3 if self.settings.winning_formula else 5
        if cb_on and self._consecutive_losses >= limit and not paused:
            if self._pause_cb:
                self._pause_cb(True)
            msg = (
                f"⚠️ CIRCUIT BREAKER: {limit} consecutive losses — new buys paused. "
                "Time until trading starts again: ~45m (or /resume sooner)."
            )
            logger.warning(msg)
            if self._notify_cb:
                self._notify_cb(msg)

    def check_exposure(
        self,
        open_exposure_usd: float,
        *,
        proposed_notional: Optional[float] = None,
        available_cash: Optional[float] = None,
    ) -> RiskVerdict:
        max_total = float(self.settings.max_total_exposure_usd)  # fixed; never equity-scaled
        max_trade = float(self.settings.max_notional_per_trade_usd or 0)
        remaining = max_total - max(0.0, open_exposure_usd)
        proposed = float(
            proposed_notional if proposed_notional is not None else max_trade
        )
        if remaining <= 1e-6:
            return RiskVerdict(
                approved=False,
                reason=(
                    f"⛔ ENTRY SKIPPED: Max exposure cap "
                    f"(${max_total:.0f}) — open ${open_exposure_usd:.0f}"
                ),
            )
        cash = float(available_cash) if available_cash is not None else None
        if cash is not None and cash <= 1e-6:
            return RiskVerdict(
                approved=False,
                reason=f"⛔ ENTRY SKIPPED: No cash available (${cash:.2f})",
            )
        # Always clamp to per-trade cap + remaining book — never all-in equity
        sized = min(proposed, remaining, max_trade if max_trade > 0 else proposed)
        if cash is not None:
            sized = min(sized, cash)
        if sized < 10.0:  # dust floor
            return RiskVerdict(
                approved=False,
                reason=(
                    f"⛔ ENTRY SKIPPED: Sized ${sized:.2f} too small "
                    f"(cash/exposure room)"
                ),
            )
        return RiskVerdict(approved=True, sized_notional=float(sized))

    def check_entry(
        self,
        *,
        open_exposure_usd: float,
        open_positions: int,
        max_concurrent: int,
        paused: bool,
        score: float,
        threshold: float,
        proposed_notional: Optional[float] = None,
        available_cash: Optional[float] = None,
    ) -> RiskVerdict:
        if paused:
            return RiskVerdict(approved=False, reason="Paused: new buys skipped")
        if open_positions >= max_concurrent:
            return RiskVerdict(
                approved=False,
                reason=f"Max concurrent positions ({max_concurrent})",
            )
        if score < threshold:
            return RiskVerdict(
                approved=False,
                reason=f"Score {score:.0f} < threshold {threshold:.0f}",
            )
        return self.check_exposure(
            open_exposure_usd,
            proposed_notional=proposed_notional,
            available_cash=available_cash,
        )
