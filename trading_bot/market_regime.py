"""BTC spot regime: BEAR_CHOP / BULL_OK (+ dump_30m SHORT bias)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from trading_bot.utils.indicators import ema

logger = logging.getLogger(__name__)


@dataclass
class BtcRegimeState:
    regime: str  # BEAR_CHOP | BULL_OK
    short_bias: bool = False
    dump_30m: bool = False
    btc_close: float = 0.0
    ema50: float = 0.0
    label: str = ""


class BtcRegimeEngine:
    """BEAR_CHOP if BTC 15m close < EMA(50); else BULL_OK.
    dump_30m if BTC dropped > ~1.2% in ~30m → SHORT bias with BEAR_CHOP.
    """

    DUMP_PCT = 0.012  # 1.2%

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._closes_15m: List[float] = []
        self.state = BtcRegimeState(regime="BULL_OK", label="BULL_OK")

    def update(self, closes_15m: Sequence[float]) -> BtcRegimeState:
        self._closes_15m = list(closes_15m)
        if not self.enabled or len(self._closes_15m) < 2:
            self.state = BtcRegimeState(regime="BULL_OK", label="BULL_OK (regime off/warmup)")
            return self.state

        close = float(self._closes_15m[-1])
        e50 = ema(self._closes_15m, 50)
        dump = False
        # ~30m ≈ 2 bars of 15m
        if len(self._closes_15m) >= 3:
            ref = float(self._closes_15m[-3])
            if ref > 0 and (ref - close) / ref > self.DUMP_PCT:
                dump = True

        if dump or close < e50:
            regime = "BEAR_CHOP"
            short_bias = True
        else:
            regime = "BULL_OK"
            short_bias = False

        label = regime
        if dump:
            label = f"{regime}+dump_30m"

        self.state = BtcRegimeState(
            regime=regime,
            short_bias=short_bias,
            dump_30m=dump,
            btc_close=close,
            ema50=e50,
            label=label,
        )
        return self.state
