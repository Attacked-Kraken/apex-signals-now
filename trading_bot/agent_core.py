"""Scan orchestration — strategy + regime + risk gates."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Tuple

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
        conc = int(getattr(settings, "ohlc_fetch_concurrency", 3) or 3)
        self._ohlc_sem = asyncio.Semaphore(max(1, conc))

    async def _score_one(
        self,
        sym: str,
        *,
        threshold: float,
        short_bias: bool,
        allow_shorts: bool,
    ) -> Signal:
        async with self._ohlc_sem:
            bars = await self.data_feed.get_ohlc(sym, interval=5)
        if len(bars) < 30:
            return Signal(symbol=sym, side="WAIT", score=0.0, reason="no bars")
        closes = DataFeed.series(bars, "c")
        volumes = DataFeed.series(bars, "v")
        highs = DataFeed.series(bars, "h")
        lows = DataFeed.series(bars, "l")
        return self.strategy.score_symbol(
            sym,
            closes,
            volumes,
            highs,
            lows,
            threshold=threshold,
            short_bias=short_bias,
            allow_shorts=allow_shorts,
        )

    async def scan(
        self,
        symbols: List[str],
        *,
        threshold: float,
        short_bias: bool = False,
    ) -> Tuple[List[Signal], float]:
        breaker = getattr(self.data_feed, "breaker", None)
        if breaker is not None and breaker.cooling_down():
            rem = breaker.remaining_seconds()
            logger.warning(
                "scan skipped: rate-limit cooldown %.0fs remaining (%s)",
                rem,
                breaker.trip_reason() or "cooling down",
            )
            return [], 0.0

        t0 = time.perf_counter()
        allow_shorts = bool(getattr(self.settings, "allow_paper_shorts", False))
        signals = await asyncio.gather(
            *[
                self._score_one(
                    sym,
                    threshold=threshold,
                    short_bias=short_bias,
                    allow_shorts=allow_shorts,
                )
                for sym in symbols
            ]
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return list(signals), elapsed_ms
