"""volume_sweet_spot — 5m low-volume retest / maker sweet spot (simplified but gated)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from trading_bot.models import Signal
from trading_bot.strategy import detect_macro_regime, normalize_market_regime
from trading_bot.utils.indicators import atr, sma


class VolumeSweetSpotStrategy:
    """Maker POST_ONLY oriented score. Respects HIGH_VOLATILITY gate & thresholds."""

    def __init__(self, settings: Any):
        self.settings = settings

    def on_stop_loss_change(self, key: str, preset: Dict[str, Any]) -> None:
        pass

    def score_symbol(
        self,
        symbol: str,
        closes: Sequence[float],
        volumes: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        *,
        threshold: float,
        short_bias: bool = False,
        allow_shorts: bool = False,
    ) -> Signal:
        if len(closes) < 30 or len(volumes) < 30:
            return Signal(symbol=symbol, side="WAIT", score=0.0, reason="warmup")

        regime = normalize_market_regime(detect_macro_regime(highs, lows, closes))
        if regime == "HIGH_VOLATILITY" and getattr(self.settings, "regime_gate_enabled", True):
            return Signal(
                symbol=symbol,
                side="WAIT",
                score=0.0,
                reason="HIGH_VOLATILITY gate (block 5m retest BUY)",
                meta={"macro": regime},
            )

        vol_sma = sma(volumes, 20)
        last_vol = float(volumes[-1])
        rvol = (last_vol / vol_sma) if vol_sma > 0 else 1.0
        price = float(closes[-1])
        ma = sma(closes, 20)
        prev = float(closes[-2])
        a = atr(highs, lows, closes, 14)

        # Sweet spot: pullback toward MA with subdued volume (retest), then stabilize
        pullback = ma > 0 and price < ma and price > ma * 0.985
        low_vol = rvol <= float(getattr(self.settings, "rvol_breakout_mult", 2.0))
        bounce = price >= prev

        score = 20.0
        if pullback:
            score += 25.0
        if low_vol:
            score += 20.0
        if bounce:
            score += 15.0
        # proximity to MA bonus
        if ma > 0:
            dist = abs(price - ma) / ma
            if dist < 0.01:
                score += 15.0
            elif dist < 0.02:
                score += 8.0
        if a > 0 and ma > 0 and (a / ma) < 0.015:
            score += 5.0

        score = min(95.0, score)
        side = "WAIT"
        reason = f"macro={regime} rvol={rvol:.2f}"
        if score >= threshold:
            if short_bias and not allow_shorts:
                # spot long-only: still allow LONG evaluation (quality gate), not hard freeze
                side = "BUY"
                reason += " SHORT-bias quality LONG"
            else:
                side = "BUY"
                reason += " sweet-spot long"
        return Signal(
            symbol=symbol,
            side=side,
            score=score,
            reason=reason,
            meta={"macro": regime, "rvol": rvol, "atr": a},
        )
