"""Scan orchestration — strategy + regime + risk gates."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from trading_bot.data_feed import DataFeed
from trading_bot.models import Signal
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotStrategy

logger = logging.getLogger(__name__)


class AgentCore:
    def __init__(
        self,
        settings: Any,
        data_feed: DataFeed,
        strategy: Optional[VolumeSweetSpotStrategy] = None,
    ):
        self.settings = settings
        self.data_feed = data_feed
        self.strategy = strategy or VolumeSweetSpotStrategy(settings)

    async def scan(
        self,
        symbols: List[str],
        *,
        threshold: float,
        short_bias: bool = False,
    ) -> Tuple[List[Signal], float]:
        t0 = time.perf_counter()
        signals: List[Signal] = []
        allow_shorts = bool(getattr(self.settings, "allow_paper_shorts", False))
        for sym in symbols:
            bars = await self.data_feed.get_ohlc(sym, interval=5)
            if len(bars) < 30:
                signals.append(Signal(symbol=sym, side="WAIT", score=0.0, reason="no bars"))
                continue
            closes = DataFeed.series(bars, "c")
            volumes = DataFeed.series(bars, "v")
            highs = DataFeed.series(bars, "h")
            lows = DataFeed.series(bars, "l")
            sig = self.strategy.score_symbol(
                sym,
                closes,
                volumes,
                highs,
                lows,
                threshold=threshold,
                short_bias=short_bias,
                allow_shorts=allow_shorts,
            )
            signals.append(sig)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return signals, elapsed_ms
