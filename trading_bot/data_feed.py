"""Bars / public market data helpers (Kraken OHLC)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from trading_bot.utils.http_errors import RateLimitError, raise_for_rate_limit
from trading_bot.utils.rate_breaker import RateLimitBreaker
from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)


class DataFeed:
    def __init__(
        self,
        base_url: str = "https://api.kraken.com",
        *,
        min_interval: float = 0.2,
        cache_ttl: float = 8.0,
        breaker: Optional[RateLimitBreaker] = None,
        live_safety: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.cache_ttl = float(cache_ttl)
        self.breaker = breaker
        self.live_safety = live_safety
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, Tuple[float, List[Dict[str, float]]]] = {}

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"User-Agent": "ApexSignalsNow/2.0 (+data-feed)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def _pair(self, symbol: str) -> str:
        base, quote = symbol.replace("/", "-").split("-")
        aliases = {"BTC": "XBT", "DOGE": "XDG"}
        base = aliases.get(base, base)
        return f"{base}{quote}"

    async def get_ohlc(self, symbol: str, interval: int = 5) -> List[Dict[str, float]]:
        """Return list of {o,h,l,c,v,t} bars. Cached briefly."""
        cache_key = f"{symbol}:{interval}"
        now = time.time()
        hit = self._cache.get(cache_key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]

        await self._throttle()
        pair = self._pair(symbol)

        def _on_rl(exc: RateLimitError) -> None:
            if self.breaker is not None:
                self.breaker.record_rate_limit(exc.retry_after)

        async def _do() -> List[Dict[str, float]]:
            client = await self._client_get()
            r = await client.get("/0/public/OHLC", params={"pair": pair, "interval": interval})
            raise_for_rate_limit(r, label=f"ohlc:{symbol}")
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            result = data.get("result") or {}
            key = next(k for k in result.keys() if k != "last")
            bars = []
            for row in result[key]:
                # [time, open, high, low, close, vwap, volume, count]
                bars.append(
                    {
                        "t": float(row[0]),
                        "o": float(row[1]),
                        "h": float(row[2]),
                        "l": float(row[3]),
                        "c": float(row[4]),
                        "v": float(row[6]),
                    }
                )
            return bars

        try:
            bars = await with_exponential_backoff(
                _do, label=f"ohlc:{symbol}", on_rate_limit=_on_rl
            )
            self._cache[cache_key] = (now, bars)
            if self.breaker is not None:
                self.breaker.record_success()
            if self.live_safety is not None:
                self.live_safety.note_connectivity_ok()
            return bars
        except RateLimitError as exc:
            # breaker already updated via on_rate_limit during retries
            logger.warning("OHLC %s rate-limited: %s", symbol, exc)
            return hit[1] if hit else []
        except Exception as exc:  # noqa: BLE001
            if self.live_safety is not None:
                self.live_safety.note_exception(exc, source=f"ohlc:{symbol}")
            logger.warning("OHLC %s failed: %s", symbol, exc)
            return hit[1] if hit else []

    @staticmethod
    def series(bars: Sequence[Dict[str, float]], key: str) -> List[float]:
        return [float(b[key]) for b in bars]
