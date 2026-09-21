"""Telegram / log notifier."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, telegram: Any = None):
        self.telegram = telegram

    async def send(self, text: str) -> None:
        logger.info("NOTIFY: %s", text[:200])
        if self.telegram is not None:
            try:
                await self.telegram.send_message(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify failed: %s", exc)

    def performance_report(self, balances: dict) -> str:
        cash = balances.get("cash", 0)
        equity = balances.get("equity", 0)
        wins = balances.get("wins", 0)
        losses = balances.get("losses", 0)
        day_start = balances.get("day_start_equity", equity)
        day_pnl = equity - day_start
        return (
            f"Day P&L: ${day_pnl:+.2f}\n"
            f"cash=${cash:,.2f} equity=${equity:,.2f}\n"
            f"closed: {wins}W/{losses}L"
        )
