"""Shared dataclasses / models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    side: str = "long"  # spot long-only default
    sl: Optional[float] = None
    tp: Optional[float] = None
    opened_at: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return abs(self.qty * self.entry)

    def unrealized_pnl_pct(self, mark: float) -> float:
        if self.entry <= 0:
            return 0.0
        if self.side == "short":
            return (self.entry - mark) / self.entry
        return (mark - self.entry) / self.entry


@dataclass
class RiskVerdict:
    approved: bool
    reason: str = ""
    sized_notional: float = 0.0


@dataclass
class Signal:
    symbol: str
    side: str  # BUY / WAIT / SELL
    score: float
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    status: str = "filled"
    paper: bool = True
