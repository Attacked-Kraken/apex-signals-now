"""Order execution gated through broker (paper or live)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from trading_bot.models import OrderResult

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, broker: Any, *, dry_run: bool = False, post_only: bool = True):
        self.broker = broker
        self.dry_run = dry_run
        self.post_only = post_only

    async def buy(
        self,
        symbol: str,
        notional: float,
        *,
        price: Optional[float] = None,
    ) -> Optional[OrderResult]:
        ticker = await self.broker.get_ticker(symbol)
        px = float(price or ticker.get("ask") or ticker.get("mid") or 0)
        if px <= 0:
            logger.warning("No price for %s", symbol)
            return None
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
        return await self.broker.place_order(
            symbol, "BUY", qty, price=px, order_type="limit" if self.post_only else "market",
            post_only=self.post_only,
        )

    async def sell(
        self,
        symbol: str,
        qty: float,
        *,
        price: Optional[float] = None,
        reason: str = "",
    ) -> Optional[OrderResult]:
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
        return await self.broker.place_order(symbol, "SELL", qty, price=price, order_type="market")
