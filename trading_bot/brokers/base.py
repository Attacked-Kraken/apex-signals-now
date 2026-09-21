"""Broker abstract base."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from trading_bot.models import OrderResult, Position


class BrokerBase(ABC):
    @abstractmethod
    async def get_ticker(self, symbol: str) -> Dict[str, float]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def cancel_all(self) -> int:
        ...

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        ...

    @abstractmethod
    async def get_balances(self) -> Dict[str, Any]:
        ...
