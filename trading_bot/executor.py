"""Order execution gated through broker (paper or live)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from trading_bot.models import OrderResult
from trading_bot.utils.order_rate_limit import OrderRateLimiter

logger = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        broker: Any,
        *,
        dry_run: bool = False,
        post_only: bool = True,
        order_limiter: Optional[OrderRateLimiter] = None,
        paper: bool = True,
    ):
        self.broker = broker
        self.dry_run = dry_run
        self.post_only = post_only
        self.order_limiter = order_limiter or OrderRateLimiter(max_per_minute=6)
        self.paper = paper

    def set_paper(self, paper: bool) -> None:
        self.paper = bool(paper)

    def _live_order_guard(self) -> Optional[str]:
        if self.paper or self.dry_run:
            return None
        if not self.order_limiter.allow():
            return self.order_limiter.block_reason()
        return None

    async def buy(
        self,
        symbol: str,
        notional: float,
        *,
        price: Optional[float] = None,
    ) -> Optional[OrderResult]:
        blocked = self._live_order_guard()
        if blocked:
            logger.warning("BUY %s refused: %s", symbol, blocked)
            return None
        ticker = await self.broker.get_ticker(symbol)
        px = float(price or ticker.get("ask") or ticker.get("mid") or 0)
        if px <= 0:
            logger.warning("No price for %s", symbol)
            return None
        notional = float(notional)
        if notional <= 0:
            logger.warning("BUY %s refused: non-positive notional %.4f", symbol, notional)
            return None
        try:
            if self.paper and hasattr(self.broker, "_read_book"):
                cash = float(self.broker._read_book().get("cash") or 0)
                if cash + 1e-9 < notional:
                    notional = max(0.0, cash)
                    if notional < 10.0:
                        logger.warning("BUY %s refused: paper cash $%.2f", symbol, cash)
                        return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("cash clamp skipped: %s", exc)
        qty = notional / px
        if self.dry_run:
            logger.info("DRY_RUN BUY %s notional=%.2f qty=%.6f @ %.4f", symbol, notional, qty, px)
            return OrderResult(
                order_id="dry-run",
                symbol=symbol,
                side="BUY",
                qty=qty,
                price=px,
                status="dry_run",
                paper=True,
            )
        # Single attempt — never retry-storm live/paper place_order here
        try:
            result = await self.broker.place_order(
                symbol,
                "BUY",
                qty,
                price=px,
                order_type="limit" if self.post_only else "market",
                post_only=self.post_only,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("BUY %s failed (no retry): %s", symbol, exc)
            return None
        if not self.paper and result is not None:
            self.order_limiter.record()
        return result

    async def sell(
        self,
        symbol: str,
        qty: float,
        *,
        price: Optional[float] = None,
        reason: str = "",
    ) -> Optional[OrderResult]:
        blocked = self._live_order_guard()
        if blocked:
            # Exits are safety-critical: allow 1 extra slot by only blocking when 2x over
            if self.order_limiter.remaining() <= 0:
                logger.warning("SELL %s deferred: %s", symbol, blocked)
                return None
        if self.dry_run:
            ticker = await self.broker.get_ticker(symbol)
            px = float(price or ticker.get("bid") or ticker.get("mid") or 0)
            logger.info("DRY_RUN SELL %s qty=%.6f @ %.4f (%s)", symbol, qty, px, reason)
            return OrderResult(
                order_id="dry-run",
                symbol=symbol,
                side="SELL",
                qty=qty,
                price=px,
                status="dry_run",
                paper=True,
            )
        try:
            result = await self.broker.place_order(
                symbol, "SELL", qty, price=price, order_type="market"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SELL %s failed (no retry): %s", symbol, exc)
            return None
        if not self.paper and result is not None:
            self.order_limiter.record()
        return result
