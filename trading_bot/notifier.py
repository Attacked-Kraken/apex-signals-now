"""Telegram / log notifier with New Guy backup mirror + optional SMS/email stub."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional, Sequence, Union

from trading_bot.backup_comms import (
    CRITICAL_SMS_MAX,
    CRITICAL_TG_MAX,
    email_stub_configured,
    is_critical_alert,
    notify_email_stub,
    notify_sms,
    resolve_backup_telegram,
    send_telegram_message,
    twilio_config,
)

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, telegram: Any = None):
        self.telegram = telegram
        self.backup_token: str = ""
        self.backup_chat_id: str = ""
        self.backup_enabled: bool = False
        self._backup_source: str = "none"
        self._sms_ready: bool = False
        self._email_ready: bool = False

    def configure_backup(self) -> None:
        """Load backup Telegram (New Guy) + detect SMS/email readiness. Never logs tokens."""
        token, chat, source = resolve_backup_telegram()
        self.backup_token = token
        self.backup_chat_id = chat
        self.backup_enabled = bool(token and chat)
        self._backup_source = source
        ready, _cfg = twilio_config()
        self._sms_ready = ready
        self._email_ready = email_stub_configured()
        if self.backup_enabled:
            logger.info(
                "backup telegram enabled (source=%s chat_configured=%s)",
                source,
                bool(chat),
            )
        else:
            logger.info("backup telegram disabled (no New Guy / BACKUP token)")
        if self._sms_ready:
            logger.info("SMS enabled (Twilio fully configured)")
        else:
            _to_set = bool(twilio_config()[1].get("to"))
            logger.info(
                "SMS plumbing ready — awaiting TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM (SMS_TO set=%s)",
                _to_set,
            )
        if self._email_ready:
            logger.info("email backup configured (stub)")
        else:
            logger.debug("email stub idle (BACKUP_EMAIL_TO / SMTP_* unset)")

    async def send(self, text: str) -> None:
        logger.info("NOTIFY: %s", text[:200])
        if self.telegram is not None:
            try:
                await self.telegram.send_message(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify failed: %s", exc)
        # Mirror + SMS only for critical short alerts; never break primary.
        try:
            await self._mirror_critical(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("backup mirror failed: %s", type(exc).__name__)

    async def send_backup_only(self, text: str, *, sms: bool = True) -> None:
        """Ops ping to New Guy backup (and optional SMS) without primary TG."""
        try:
            await self._mirror_critical(text, force=True, sms=sms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("backup-only send failed: %s", type(exc).__name__)

    async def _mirror_critical(
        self, text: str, *, force: bool = False, sms: bool = True
    ) -> None:
        body = (text or "").strip()
        if not body:
            return
        if not force and not is_critical_alert(body):
            return
        short = body if len(body) <= CRITICAL_TG_MAX else body[: CRITICAL_TG_MAX - 1] + "…"
        if self.backup_enabled:
            ok = await send_telegram_message(
                self.backup_token, self.backup_chat_id, short
            )
            if not ok:
                logger.warning("backup telegram send failed (primary unaffected)")
        if sms and is_critical_alert(body):
            sms_body = body if len(body) <= CRITICAL_SMS_MAX - 6 else body[: CRITICAL_SMS_MAX - 7] + "…"
            try:
                notify_sms(sms_body, prefix="Apex:")
            except Exception as exc:  # noqa: BLE001
                logger.warning("SMS mirror failed: %s", type(exc).__name__)
        if self._email_ready and is_critical_alert(body):
            try:
                notify_email_stub("Apex alert", short)
            except Exception as exc:  # noqa: BLE001
                logger.debug("email stub failed: %s", type(exc).__name__)

    def performance_report(
        self,
        balances: dict,
        *,
        trades: Optional[Sequence[dict]] = None,
        paper: bool = True,
        now: Optional[datetime] = None,
        expanded: bool = False,
    ) -> Any:
        """Production CRUZBOT PERFORMANCE REPORT (/pnl).

        Paper path: prefer paper_book closed_trades (ledger often missing).
        Falls back to session (all closed) when today's CT day filter is empty.

        Returns TelegramReply: compact (summary + last ~3 trades + ▼) by default;
        expanded=True shows the full newest-first list with ▲ collapse.
        """
        from trading_bot.telegram_commands import (
            PNL_COMPACT_PREVIEW,
            TelegramReply,
            closed_trades_to_day_rows,
            day_trades_from_ledger,
            format_performance_report,
            pnl_with_trades_button,
            sort_pnl_trades_newest_first,
        )

        cash = float(balances.get("cash", 0) or 0)
        equity = float(balances.get("equity", 0) or 0)
        closed = balances.get("closed_trades") or []
        scope = "Day"
        rows: List[dict]
        if trades is not None:
            rows = list(trades)
        else:
            # Paper book is source of truth; ledger DB is optional / often absent.
            rows = closed_trades_to_day_rows(closed, now=now)
            if not rows:
                try:
                    rows = day_trades_from_ledger(now=now)
                except TypeError:
                    rows = day_trades_from_ledger()
            if not rows and closed:
                rows = closed_trades_to_day_rows(closed, now=now, session=True)
                scope = "Session"
        rows = sort_pnl_trades_newest_first(rows)
        use_compact = (not expanded) and len(rows) > PNL_COMPACT_PREVIEW
        report = format_performance_report(
            trades=rows,
            cash=cash,
            equity=equity,
            paper=paper,
            now=now,
            scope=scope,
            compact=use_compact,
            preview_n=PNL_COMPACT_PREVIEW,
        )
        n = len(rows)
        day_net = sum(float(t.get("pnl") or 0) for t in rows)
        net_s = f"+${day_net:,.2f}" if day_net >= 0 else f"-${abs(day_net):,.2f}"
        footer = (
            f"{scope} P&L report sent ({n} closed). Net {net_s} | "
            f"cash=${cash:,.2f} equity=${equity:,.2f}"
        )
        text = report + "\n\n" + footer
        return pnl_with_trades_button(
            text, trade_count=n, expanded=bool(expanded) and n > PNL_COMPACT_PREVIEW
        )
