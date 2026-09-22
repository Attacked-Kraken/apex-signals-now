"""Kraken broker — paper mode simulates fills locally; no AddOrder/Cancel while paper on."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from trading_bot.brokers.base import BrokerBase
from trading_bot.models import OrderResult, Position, utcnow
from trading_bot.utils.http_errors import RateLimitError, raise_for_rate_limit
from trading_bot.utils.rate_breaker import RateLimitBreaker
from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)

# Public REST only while paper; private order endpoints blocked in paper mode.
_PRIVATE_ORDER_METHODS = {"AddOrder", "CancelOrder", "CancelAll", "CancelAllOrdersAfter"}


class KrakenBroker(BrokerBase):
    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.kraken.com",
        paper: bool = True,
        paper_book_path: str = "data/paper_book_2.json",
        account_equity: float = 1600.0,
        min_public_interval: float = 0.2,
        breaker: Optional[RateLimitBreaker] = None,
        live_safety: Any = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.paper = paper
        self.paper_book_path = Path(paper_book_path)
        self.account_equity = float(account_equity)
        self.min_public_interval = min_public_interval
        self.breaker = breaker
        self.live_safety = live_safety
        self._last_public = 0.0
        self._client: Optional[httpx.AsyncClient] = None
        self._ticker_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}
        # Reuse marks across /status + balances + scan (was 1s → frequent refetches).
        self._ticker_cache_ttl = 4.0
        # Longer stale-ok window for /status (prefer last-known over blocking).
        self._status_mark_max_age = 20.0
        self._ensure_book(account_equity)

    def _ensure_book(self, equity: float) -> None:
        self.paper_book_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.paper_book_path.exists():
            self._write_book(
                {
                    "cash": float(equity),
                    "positions": {},
                    "closed_trades": [],
                    "consecutive_losses": 0,
                    "day_start_equity": float(equity),
                    "wallet_b4": float(equity),
                    "updated_at": utcnow().isoformat(),
                }
            )

    def _read_book(self) -> Dict[str, Any]:
        with open(self.paper_book_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_book(self, book: Dict[str, Any]) -> None:
        book["updated_at"] = utcnow().isoformat()
        tmp = self.paper_book_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(book, f, indent=2)
        tmp.replace(self.paper_book_path)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"User-Agent": "ApexSignalsNow/2.0 (+paper-kraken)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle_public(self) -> None:
        now = time.monotonic()
        wait = self.min_public_interval - (now - self._last_public)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_public = time.monotonic()

    def _to_kraken_pair(self, symbol: str) -> str:
        # BTC-USD -> XBTUSD style for public ticker (simplified map)
        base, quote = symbol.replace("/", "-").split("-")
        aliases = {"BTC": "XBT", "DOGE": "XDG"}
        base = aliases.get(base, base)
        return f"{base}{quote}"

    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        await self._throttle_public()

        def _on_rl(exc: RateLimitError) -> None:
            if self.breaker is not None:
                self.breaker.record_rate_limit(exc.retry_after)

        async def _do() -> Dict[str, Any]:
            client = await self._get_client()
            r = await client.get(path, params=params or {})
            raise_for_rate_limit(r, label=f"kraken_public:{path}")
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"Kraken public error: {data['error']}")
            return data.get("result") or data

        result = await with_exponential_backoff(
            _do, label=f"kraken_public:{path}", on_rate_limit=_on_rl
        )
        if self.breaker is not None:
            self.breaker.record_success()
        return result

    async def _private_order_blocked(self, method: str) -> None:
        if self.paper and method in _PRIVATE_ORDER_METHODS:
            raise RuntimeError(
                f"PAPER_TRADING_MODE=true: refusing Kraken {method} "
                "(orders simulated locally as kr-paper-…)"
            )

    def note_private_api_result(self, error: Any = None, *, status_code: Optional[int] = None) -> str:
        """Feed future signed-private results into LIVE safety without logging secrets."""
        if self.live_safety is None:
            return "other"
        if error is None:
            self.live_safety.note_private_ok()
            return "ok"
        return self.live_safety.note_exception(
            error, source="kraken:private", status_code=status_code
        )

    def private_signing_available(self) -> bool:
        """Whether a reviewed Kraken private signing transport is available.

        Credentials alone are intentionally insufficient. This paper-first broker has
        no private HMAC request implementation, so this remains False until that path
        is implemented and tested.
        """
        return False

    def private_live_path_complete(self) -> bool:
        """True only after signed AddOrder/Cancel are implemented (not yet)."""
        return False

    def dead_man_status(self) -> str:
        from trading_bot.utils.live_safety import DEADMAN_NOT_ARMED, format_dead_man_status

        if not self.private_live_path_complete():
            return DEADMAN_NOT_ARMED
        return format_dead_man_status(
            paper=self.paper,
            signing_available=self.private_signing_available(),
            private_path_complete=True,
        )

    async def cancel_all_orders_after(self, timeout_seconds: int = 60) -> Dict[str, Any]:
        """Kraken CancelAllOrdersAfter dead-man heartbeat interface.

        This method deliberately issues *no* private request until reviewed signing,
        live AddOrder, and live Cancel exist. It never fakes an armed/success state.
        """
        timeout = int(timeout_seconds)
        if timeout != 60:
            return {
                "ok": False,
                "armed": False,
                "timeout": timeout,
                "status": "not armed — heartbeat must be exactly 60s",
            }
        return {
            "ok": False,
            "armed": False,
            "timeout": timeout,
            "status": self.dead_man_status(),
        }

    @staticmethod
    def compute_daily_pct(last: Any, day_open: Any) -> Optional[float]:
        """UTC-session daily %: (last - day_open) / day_open * 100."""
        try:
            last_f = float(last)
            open_f = float(day_open)
        except (TypeError, ValueError):
            return None
        if open_f <= 0 or last_f <= 0:
            return None
        return (last_f - open_f) / open_f * 100.0

    @staticmethod
    def _parse_day_open(open_raw: Any) -> Tuple[float, float]:
        """Kraken `o` is usually a string (today open), sometimes [today, 24h].

        Never index a string — o[0] on "86598" is the char "8".
        """
        open_px = 0.0
        open_24h = 0.0
        try:
            if isinstance(open_raw, str):
                open_px = float(open_raw or 0)
                open_24h = open_px
            elif isinstance(open_raw, (list, tuple)):
                if open_raw:
                    open_px = float(open_raw[0] or 0)
                if len(open_raw) > 1:
                    open_24h = float(open_raw[1] or 0)
                else:
                    open_24h = open_px
            elif open_raw is not None:
                open_px = float(open_raw or 0)
                open_24h = open_px
        except (TypeError, ValueError):
            return 0.0, 0.0
        return open_px, open_24h

    @staticmethod
    def _parse_ticker_row(row: Dict[str, Any]) -> Dict[str, float]:
        """Parse Kraken Ticker row → bid/ask/last/mid + session open/daily_pct."""
        bid = float(row["b"][0])
        ask = float(row["a"][0])
        last = float(row["c"][0])
        open_px, open_24h = KrakenBroker._parse_day_open(row.get("o"))
        daily_pct = KrakenBroker.compute_daily_pct(last, open_px)
        out: Dict[str, float] = {
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": (bid + ask) / 2.0,
            "open": open_px,
            "open_24h": open_24h,
        }
        # Always set key so cache hits are known to include daily fields.
        out["daily_pct"] = daily_pct  # type: ignore[assignment]
        return out

    @staticmethod
    def _ticker_has_daily(tick: Optional[Dict[str, float]]) -> bool:
        """True only when day open or a real daily_pct is present (not OHLC-only)."""
        if not isinstance(tick, dict):
            return False
        if tick.get("daily_pct") is not None:
            return True
        try:
            return float(tick.get("open") or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _map_ticker_result_key(
        key: str, pair_to_sym: Dict[str, str]
    ) -> Optional[str]:
        """Map Kraken result key (XXBTZUSD / SOLUSD) → our symbol."""
        if key in pair_to_sym:
            return pair_to_sym[key]
        candidates = [key]
        for zquote, quote in (
            ("ZUSD", "USD"),
            ("ZEUR", "EUR"),
            ("ZGBP", "GBP"),
            ("ZCAD", "CAD"),
            ("ZJPY", "JPY"),
        ):
            if key.startswith("X") and key.endswith(zquote) and len(key) > len(zquote) + 1:
                base = key[1 : -len(zquote)]  # XXBTZUSD→XBT, XETHZUSD→ETH
                candidates.append(base + quote)
                break
        for cand in candidates:
            hit = pair_to_sym.get(cand)
            if hit is not None:
                return hit
        for pk, sym in pair_to_sym.items():
            if key.endswith(pk) or pk in key or key in pk:
                return sym
        return None

    def peek_cached_ticker(
        self, symbol: str, *, max_age: Optional[float] = None
    ) -> Optional[Dict[str, float]]:
        """Return a cached ticker if younger than max_age (default: status window)."""
        hit = self._ticker_cache.get(symbol)
        if not hit:
            return None
        age_limit = self._ticker_cache_ttl if max_age is None else float(max_age)
        if time.time() - hit[0] < age_limit:
            return hit[1]
        return None

    def cache_ticker(self, symbol: str, tick: Dict[str, float]) -> None:
        """Seed/refresh ticker cache (e.g. OHLC last close for /status).

        Never let an OHLC close-only seed wipe a prior day-open / daily_pct.
        When open is preserved and last updates, recompute daily %.
        """
        merged = dict(tick)
        prev = self._ticker_cache.get(symbol)
        if prev and not self._ticker_has_daily(merged) and self._ticker_has_daily(prev[1]):
            for k in ("open", "open_24h"):
                if k in prev[1] and k not in merged:
                    merged[k] = prev[1][k]
        last = merged.get("last") or merged.get("mid")
        open_px = merged.get("open")
        recomputed = self.compute_daily_pct(last, open_px)
        if recomputed is not None:
            merged["daily_pct"] = recomputed  # type: ignore[assignment]
        elif "daily_pct" not in merged and prev and prev[1].get("daily_pct") is not None:
            merged["daily_pct"] = prev[1]["daily_pct"]
        self._ticker_cache[symbol] = (time.time(), merged)

    async def get_ticker(self, symbol: str) -> Dict[str, float]:
        now = time.time()
        hit = self._ticker_cache.get(symbol)
        if (
            hit
            and now - hit[0] < self._ticker_cache_ttl
            and self._ticker_has_daily(hit[1])
        ):
            return hit[1]
        pair = self._to_kraken_pair(symbol)
        try:
            result = await self._public_get("/0/public/Ticker", {"pair": pair})
            # result keys vary; take first
            key = next(iter(result))
            row = result[key]
            out = self._parse_ticker_row(row)
            self._ticker_cache[symbol] = (now, out)
            if self.live_safety is not None:
                self.live_safety.note_ticker_ok(symbol)
            return out
        except Exception as exc:  # noqa: BLE001
            if self.live_safety is not None:
                self.live_safety.note_exception(exc, source=f"ticker:{symbol}")
            logger.warning("ticker %s failed: %s — using book fallback", symbol, exc)
            book = self._read_book()
            pos = book.get("positions", {}).get(symbol)
            if pos:
                px = float(pos.get("entry", 0) or 0)
                return {"bid": px, "ask": px, "last": px, "mid": px}
            # dry synthetic
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "mid": 0.0}

    async def get_tickers(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Batch public Ticker for many symbols in one request (faster marks)."""
        out: Dict[str, Dict[str, float]] = {}
        need: List[str] = []
        now = time.time()
        for sym in symbols:
            hit = self._ticker_cache.get(sym)
            if (
                hit
                and now - hit[0] < self._ticker_cache_ttl
                and self._ticker_has_daily(hit[1])
            ):
                out[sym] = hit[1]
            else:
                need.append(sym)
        if not need:
            return out
        pairs = ",".join(self._to_kraken_pair(s) for s in need)
        pair_to_sym = {self._to_kraken_pair(s): s for s in need}
        try:
            result = await self._public_get("/0/public/Ticker", {"pair": pairs})
            for key, row in (result or {}).items():
                # Kraken may return altname keys; map best-effort
                sym = self._map_ticker_result_key(key, pair_to_sym)
                if sym is None:
                    continue
                tick = self._parse_ticker_row(row)
                self._ticker_cache[sym] = (now, tick)
                out[sym] = tick
                if self.live_safety is not None:
                    self.live_safety.note_ticker_ok(sym)
            for sym in need:
                if sym not in out:
                    out[sym] = await self.get_ticker(sym)
        except Exception as exc:  # noqa: BLE001
            if self.live_safety is not None:
                self.live_safety.note_exception(exc, source="ticker:batch")
            logger.warning("batch ticker failed: %s — falling back per-symbol", exc)
            for sym in need:
                out[sym] = await self.get_ticker(sym)
        return out

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        price: Optional[float] = None,
        order_type: str = "market",
        post_only: bool = False,
        reason: str = "",
        peak_upl_pct: Optional[float] = None,
    ) -> OrderResult:
        side_u = side.upper()
        if self.paper:
            return await self._paper_fill(
                symbol,
                side_u,
                qty,
                price=price,
                reason=reason,
                peak_upl_pct=peak_upl_pct,
            )
        await self._private_order_blocked("AddOrder")
        raise NotImplementedError("Live AddOrder not enabled in this paper-first skeleton")

    async def _paper_fill(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        price: Optional[float] = None,
        reason: str = "",
        peak_upl_pct: Optional[float] = None,
    ) -> OrderResult:
        ticker = await self.get_ticker(symbol)
        px = float(price) if price is not None and price > 0 else float(ticker.get("mid") or ticker.get("last") or 0)
        if px <= 0:
            raise RuntimeError(f"No mark for {symbol}; cannot paper fill")

        book = self._read_book()
        cash = float(book.get("cash", 0))
        positions: Dict[str, Any] = book.setdefault("positions", {})
        oid = f"kr-paper-{uuid.uuid4().hex[:12]}"

        if side in ("BUY", "buy"):
            cost = qty * px
            if cost > cash + 1e-9:
                raise RuntimeError(f"Insufficient paper cash: need ${cost:.2f}, have ${cash:.2f}")
            book["cash"] = cash - cost
            if symbol in positions:
                prev = positions[symbol]
                prev_qty = float(prev["qty"])
                prev_entry = float(prev["entry"])
                new_qty = prev_qty + qty
                new_entry = ((prev_qty * prev_entry) + cost) / new_qty if new_qty else px
                prev["qty"] = new_qty
                prev["entry"] = new_entry
            else:
                positions[symbol] = {
                    "symbol": symbol,
                    "qty": qty,
                    "entry": px,
                    "side": "long",
                    "sl": None,
                    "tp": None,
                    "opened_at": utcnow().isoformat(),
                    "meta": {},
                }
        else:  # SELL / close
            if symbol not in positions:
                raise RuntimeError(f"No paper position for {symbol}")
            pos = positions[symbol]
            pos_qty = float(pos["qty"])
            sell_qty = min(qty, pos_qty)
            entry = float(pos["entry"])
            proceeds = sell_qty * px
            pnl = (px - entry) * sell_qty
            book["cash"] = cash + proceeds
            remaining = pos_qty - sell_qty
            # peak_upl_pct: percent units (1.25 == +1.25%), converted from ops.extra
            # fraction peak_upl when provided by the exit path. None if unknown.
            closed = {
                "symbol": symbol,
                "qty": sell_qty,
                "entry": entry,
                "exit": px,
                "pnl": pnl,
                "won": pnl > 0,
                "closed_at": utcnow().isoformat(),
                "order_id": oid,
                "reason": reason or "",
                "peak_upl_pct": (
                    float(peak_upl_pct) if peak_upl_pct is not None else None
                ),
            }
            book.setdefault("closed_trades", []).append(closed)
            if remaining <= 1e-12:
                del positions[symbol]
            else:
                pos["qty"] = remaining

        self._write_book(book)
        logger.info("PAPER FILL %s %s qty=%.6f @ %.4f id=%s", side, symbol, qty, px, oid)
        return OrderResult(
            order_id=oid,
            symbol=symbol,
            side=side,
            qty=qty,
            price=px,
            status="filled",
            paper=True,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if self.paper:
            logger.info("PAPER cancel_order noop: %s", order_id)
            return True
        await self._private_order_blocked("CancelOrder")
        return False

    async def cancel_all(self) -> int:
        if self.paper:
            logger.info("PAPER cancel_all noop")
            return 0
        await self._private_order_blocked("CancelAll")
        return 0

    async def get_positions(self) -> List[Position]:
        book = self._read_book()
        out: List[Position] = []
        for sym, raw in (book.get("positions") or {}).items():
            out.append(
                Position(
                    symbol=sym,
                    qty=float(raw["qty"]),
                    entry=float(raw["entry"]),
                    side=str(raw.get("side") or "long"),
                    sl=raw.get("sl"),
                    tp=raw.get("tp"),
                    opened_at=raw.get("opened_at"),
                    meta=dict(raw.get("meta") or {}),
                )
            )
        return out

    async def update_position_brackets(
        self, symbol: str, *, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> None:
        book = self._read_book()
        pos = book.get("positions", {}).get(symbol)
        if not pos:
            return
        if sl is not None:
            pos["sl"] = sl
        if tp is not None:
            pos["tp"] = tp
        self._write_book(book)

    async def get_balances(
        self,
        *,
        marks: Optional[Dict[str, Dict[str, float]]] = None,
        prefer_cache_max_age: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Paper book balances. Marks: one batch fetch (or caller-supplied / cache)."""
        book = self._read_book()
        cash = float(book.get("cash", 0))
        positions = await self.get_positions()
        exposure = 0.0
        if positions:
            if marks is None:
                syms = [p.symbol for p in positions]
                # Prefer fresh-enough cached marks to avoid serial public polls.
                age = (
                    float(prefer_cache_max_age)
                    if prefer_cache_max_age is not None
                    else float(self._status_mark_max_age)
                )
                cached: Dict[str, Dict[str, float]] = {}
                need: List[str] = []
                for sym in syms:
                    hit = self.peek_cached_ticker(sym, max_age=age)
                    if hit is not None:
                        cached[sym] = hit
                    else:
                        need.append(sym)
                if need:
                    fetched = await self.get_tickers(need)
                    cached.update(fetched)
                marks = cached
            for p in positions:
                ticker = (marks or {}).get(p.symbol) or {}
                mark = float(ticker.get("mid") or p.entry)
                exposure += abs(p.qty * mark)
        equity = cash + exposure
        closed = book.get("closed_trades") or []
        wins = sum(1 for t in closed if t.get("won") or float(t.get("pnl") or 0) > 0)
        losses = sum(1 for t in closed if not (t.get("won") or float(t.get("pnl") or 0) > 0))
        realized = 0.0
        for t in closed:
            try:
                realized += float(t.get("pnl") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "cash": cash,
            "equity": equity,
            "exposure": exposure,
            "wins": wins,
            "losses": losses,
            "realized_pnl": realized,
            "day_start_equity": float(book.get("day_start_equity", cash)),
            "consecutive_losses": int(book.get("consecutive_losses", 0)),
            "closed_trades": closed,
        }

    def reset_paper(self, equity: float = 1600.0) -> None:
        self._write_book(
            {
                "cash": float(equity),
                "positions": {},
                "closed_trades": [],
                "consecutive_losses": 0,
                "day_start_equity": float(equity),
                "wallet_b4": float(equity),
                "updated_at": utcnow().isoformat(),
            }
        )


    def paper_wallet_b4(self) -> float:
        """Baseline paper bankroll shown as Wallet B4 on /status."""
        book = self._read_book()
        for key in ("wallet_b4", "day_start_equity", "cash"):
            if key in book and book[key] is not None:
                try:
                    return float(book[key])
                except (TypeError, ValueError):
                    pass
        return float(self.account_equity or 0)

    def set_consecutive_losses(self, n: int) -> None:
        book = self._read_book()
        book["consecutive_losses"] = int(n)
        self._write_book(book)
