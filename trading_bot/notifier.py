"""Telegram / log notifier."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional, Sequence

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

    def performance_report(
        self,
        balances: dict,
        *,
        trades: Optional[Sequence[dict]] = None,
        paper: bool = True,
        now: Optional[datetime] = None,
    ) -> str:
        """Production CRUZBOT PERFORMANCE REPORT (/pnl)."""
        from trading_bot.telegram_commands import (
            closed_trades_to_day_rows,
            day_trades_from_ledger,
            format_performance_report,
        )

        cash = float(balances.get("cash", 0) or 0)
        equity = float(balances.get("equity", 0) or 0)
        rows: List[dict]
        if trades is not None:
            rows = list(trades)
        else:
            rows = day_trades_from_ledger()
            if not rows:
                rows = closed_trades_to_day_rows(
                    balances.get("closed_trades") or [], now=now
                )
        report = format_performance_report(
            trades=rows, cash=cash, equity=equity, paper=paper, now=now
        )
        n = len(rows)
        day_net = sum(float(t.get("pnl") or 0) for t in rows)
        net_s = f"+${day_net:,.2f}" if day_net >= 0 else f"-${abs(day_net):,.2f}"
        footer = (
            f"Day P&L report sent ({n} closed). Net {net_s} | "
            f"cash=${cash:,.2f} equity=${equity:,.2f}"
        )
        return report + "\n\n" + footer
