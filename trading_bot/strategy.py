"""Macro ATR+ADX regime: TRENDING / RANGING / HIGH_VOLATILITY."""
from __future__ import annotations

from typing import Sequence, Tuple

from trading_bot.utils.indicators import adx_proxy, atr


def normalize_market_regime(raw: str) -> str:
    """Alias RANGE → RANGING. No BULL_TREND string — bullish BTC is BULL_OK."""
    key = (raw or "").strip().upper().replace(" ", "_")
    if key in ("RANGE", "RANGING"):
        return "RANGING"
    if key in ("TREND", "TRENDING"):
        return "TRENDING"
    if key in ("HIGH_VOL", "HIGH_VOLATILITY", "HV"):
        return "HIGH_VOLATILITY"
    if key in ("TRENDING", "RANGING", "HIGH_VOLATILITY"):
        return key
    return "RANGING"


def detect_macro_regime(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    atr_period: int = 14,
    high_vol_atr_pct: float = 0.025,
    trend_adx: float = 25.0,
) -> str:
    if len(closes) < atr_period + 2:
        return "RANGING"
    a = atr(highs, lows, closes, atr_period)
    mid = float(closes[-1]) or 1.0
    atr_pct = a / mid
    strength = adx_proxy(highs, lows, closes, atr_period)
    if atr_pct >= high_vol_atr_pct:
        return "HIGH_VOLATILITY"
    if strength >= trend_adx:
        return "TRENDING"
    return "RANGING"


def check_daily_drawdown_circuit(
    day_pnl_usd: float,
    day_start_equity: float,
    *,
    limit_pct: float = 0.03,
) -> Tuple[bool, str]:
    """Return (blocked, reason) if day SQLite PnL ≤ −3% of day-start equity."""
    if day_start_equity <= 0:
        return False, ""
    dd = day_pnl_usd / day_start_equity
    if dd <= -limit_pct:
        return True, f"Daily DD circuit: {dd*100:.2f}% ≤ −{limit_pct*100:.0f}%"
    return False, ""
