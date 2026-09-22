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
        soft_ttl_mult: float = 3.0,
        breaker: Optional[RateLimitBreaker] = None,
        live_safety: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.cache_ttl = float(cache_ttl)
        self.soft_ttl_mult = max(1.0, float(soft_ttl_mult))
        self.breaker = breaker
        self.live_safety = live_safety
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, Tuple[float, List[Dict[str, float]]]] = {}
        self._inflight: Dict[str, asyncio.Task] = {}

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"User-Agent": "ApexSignalsNow/2.0 (+data-feed)"},
            )
        return self._client

    async def close(self) -> None:
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
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

    def peek_ohlc(
        self, symbol: str, interval: int = 5, *, max_age: Optional[float] = None
    ) -> Optional[List[Dict[str, float]]]:
        """Return cached bars if present and younger than max_age (no network)."""
        cache_key = f"{symbol}:{interval}"
        hit = self._cache.get(cache_key)
        if not hit:
            return None
        age_limit = self.cache_ttl if max_age is None else float(max_age)
        if time.time() - hit[0] < age_limit:
            return hit[1]
        return None

    async def _fetch_ohlc(self, symbol: str, interval: int) -> List[Dict[str, float]]:
        """Network fetch + cache write. Caller handles throttle/errors."""
        cache_key = f"{symbol}:{interval}"
        await self._throttle()
        pair = self._pair(symbol)
        prev = self._cache.get(cache_key)

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
            self._cache[cache_key] = (time.time(), bars)
            if self.breaker is not None:
                self.breaker.record_success()
            if self.live_safety is not None:
                self.live_safety.note_connectivity_ok()
            return bars
        except RateLimitError as exc:
            logger.warning("OHLC %s rate-limited: %s", symbol, exc)
            return prev[1] if prev else []
        except Exception as exc:  # noqa: BLE001
            if self.live_safety is not None:
                self.live_safety.note_exception(exc, source=f"ohlc:{symbol}")
            logger.warning("OHLC %s failed: %s", symbol, exc)
            return prev[1] if prev else []

    def _kick_background_refresh(self, symbol: str, interval: int) -> None:
        cache_key = f"{symbol}:{interval}"
        existing = self._inflight.get(cache_key)
        if existing is not None and not existing.done():
            return

        async def _bg() -> None:
            try:
                await self._fetch_ohlc(symbol, interval)
            finally:
                cur = self._inflight.get(cache_key)
                if cur is asyncio.current_task():
                    self._inflight.pop(cache_key, None)

        try:
            self._inflight[cache_key] = asyncio.create_task(_bg())
        except RuntimeError:
            # No running loop (sync context) — skip background refresh.
            pass

    async def get_ohlc(self, symbol: str, interval: int = 5) -> List[Dict[str, float]]:
        """Return list of {o,h,l,c,v,t} bars.

        Fresh cache (< cache_ttl): return immediately.
        Soft-stale (< cache_ttl * soft_ttl_mult): return stale bars and refresh
        in the background so scans stay fast without blocking on Kraken.
        Hard miss / too old: await a network fetch.
        """
        cache_key = f"{symbol}:{interval}"
        now = time.time()
        hit = self._cache.get(cache_key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]

        soft_ttl = self.cache_ttl * self.soft_ttl_mult
        if hit and hit[1] and now - hit[0] < soft_ttl:
            self._kick_background_refresh(symbol, interval)
            return hit[1]

        return await self._fetch_ohlc(symbol, interval)

    async def get_ohlc_many(
        self, symbols: Sequence[str], interval: int = 5
    ) -> Dict[str, List[Dict[str, float]]]:
        """Fetch OHLC for many symbols; reuse cache / soft-stale, gather the rest."""
        out: Dict[str, List[Dict[str, float]]] = {}
        need: List[str] = []
        now = time.time()
        soft_ttl = self.cache_ttl * self.soft_ttl_mult
        for sym in symbols:
            if not sym:
                continue
            cache_key = f"{sym}:{interval}"
            hit = self._cache.get(cache_key)
            if hit and now - hit[0] < self.cache_ttl:
                out[sym] = hit[1]
            elif hit and hit[1] and now - hit[0] < soft_ttl:
                out[sym] = hit[1]
                self._kick_background_refresh(sym, interval)
            else:
                need.append(sym)
        if need:
            fetched = await asyncio.gather(
                *[self.get_ohlc(s, interval=interval) for s in need]
            )
            for sym, bars in zip(need, fetched):
                out[sym] = bars
        return out

    @staticmethod
    def series(bars: Sequence[Dict[str, float]], key: str) -> List[float]:
        return [float(b[key]) for b in bars]
