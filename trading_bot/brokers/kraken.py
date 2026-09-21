"""Kraken broker — paper mode simulates fills locally; no AddOrder/Cancel while paper on."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from trading_bot.brokers.base import BrokerBase
from trading_bot.models import OrderResult, Position, utcnow
from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)

# Public REST only while paper; private order endpoints blocked in paper mode.
_PRIVATE_ORDER_METHODS = {"AddOrder", "CancelOrder", "CancelAll"}


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
        min_public_interval: float = 0.35,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.paper = paper
        self.paper_book_path = Path(paper_book_path)
        self.account_equity = float(account_equity)
        self.min_public_interval = min_public_interval
        self._last_public = 0.0
        self._client: Optional[httpx.AsyncClient] = None
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

        async def _do() -> Dict[str, Any]:
            client = await self._get_client()
            r = await client.get(path, params=params or {})
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"Kraken public error: {data['error']}")
            return data.get("result") or data

        return await with_exponential_backoff(_do, label=f"kraken_public:{path}")

    async def _private_order_blocked(self, method: str) -> None:
        if self.paper and method in _PRIVATE_ORDER_METHODS:
            raise RuntimeError(
                f"PAPER_TRADING_MODE=true: refusing Kraken {method} "
                "(orders simulated locally as kr-paper-…)"
            )

    async def get_ticker(self, symbol: str) -> Dict[str, float]:
        pair = self._to_kraken_pair(symbol)
        try:
            result = await self._public_get("/0/public/Ticker", {"pair": pair})
            # result keys vary; take first
            key = next(iter(result))
            row = result[key]
            bid = float(row["b"][0])
            ask = float(row["a"][0])
            last = float(row["c"][0])
            return {"bid": bid, "ask": ask, "last": last, "mid": (bid + ask) / 2.0}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ticker %s failed: %s — using book fallback", symbol, exc)
            book = self._read_book()
            pos = book.get("positions", {}).get(symbol)
            if pos:
                px = float(pos.get("entry", 0) or 0)
                return {"bid": px, "ask": px, "last": px, "mid": px}
            # dry synthetic
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "mid": 0.0}

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        price: Optional[float] = None,
        order_type: str = "market",
        post_only: bool = False,
    ) -> OrderResult:
        side_u = side.upper()
        if self.paper:
            return await self._paper_fill(symbol, side_u, qty, price=price)
        await self._private_order_blocked("AddOrder")
        raise NotImplementedError("Live AddOrder not enabled in this paper-first skeleton")

    async def _paper_fill(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        price: Optional[float] = None,
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
            closed = {
                "symbol": symbol,
                "qty": sell_qty,
                "entry": entry,
                "exit": px,
                "pnl": pnl,
                "won": pnl > 0,
                "closed_at": utcnow().isoformat(),
                "order_id": oid,
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

    async def get_balances(self) -> Dict[str, Any]:
        book = self._read_book()
        cash = float(book.get("cash", 0))
        positions = await self.get_positions()
        exposure = 0.0
        for p in positions:
            ticker = await self.get_ticker(p.symbol)
            mark = float(ticker.get("mid") or p.entry)
            exposure += abs(p.qty * mark)
        equity = cash + exposure
        closed = book.get("closed_trades") or []
        wins = sum(1 for t in closed if t.get("won"))
        losses = sum(1 for t in closed if not t.get("won"))
        return {
            "cash": cash,
            "equity": equity,
            "exposure": exposure,
            "wins": wins,
            "losses": losses,
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
