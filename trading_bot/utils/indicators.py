"""Lightweight indicator helpers (no heavy pandas-ta required at import)."""
from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> float:
    if not values or period <= 0:
        return 0.0
    k = 2.0 / (period + 1)
    e = float(values[0])
    for v in values[1:]:
        e = float(v) * k + e * (1.0 - k)
    return e


def sma(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    window = list(values)[-period:]
    return sum(window) / len(window)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return 0.0
    trs = []
    for i in range(1, n):
        h, l, pc = float(highs[i]), float(lows[i]), float(closes[i - 1])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def adx_proxy(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    """Simplified ADX-like trend strength 0–100 (not exact Wilder ADX)."""
    if len(closes) < period + 1:
        return 0.0
    up_moves = 0.0
    down_moves = 0.0
    for i in range(-period, 0):
        diff = float(closes[i]) - float(closes[i - 1])
        if diff > 0:
            up_moves += diff
        else:
            down_moves += abs(diff)
    tot = up_moves + down_moves
    if tot <= 0:
        return 0.0
    dx = abs(up_moves - down_moves) / tot * 100.0
    return min(100.0, dx)
