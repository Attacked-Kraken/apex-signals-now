"""Tests for backup Telegram mirror filters + SMS plumbing gates."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_bot.backup_comms import (
    is_critical_alert,
    normalize_e164,
    notify_sms,
    resolve_backup_telegram,
    twilio_config,
)
from trading_bot.notifier import Notifier


def test_is_critical_buy_sell_win_loss():
    assert is_critical_alert("🟢 BUY BTC-USD $500 @ 100")
    assert is_critical_alert("✅ WIN TRAIL ETH-USD")
    assert is_critical_alert("❌ LOSS SL SOL-USD")
    assert is_critical_alert("CB auto-resume: cooldown done — new buys enabled")
    assert is_critical_alert("trading TG auto-heal: restarted listener")
    assert not is_critical_alert("🎯 [PROFIT RUNNER] Target reached")
    assert not is_critical_alert("heartbeat ok")


def test_normalize_e164_us_local():
    assert normalize_e164("5551234567") == "+15551234567"
    assert normalize_e164("+15551234567") == "+15551234567"
    assert normalize_e164("15551234567") == "+15551234567"
    assert normalize_e164("") == ""


def test_twilio_incomplete_skips(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_FROM_NUMBER", raising=False)
    # Even with SMS_TO set, incomplete Twilio must no-op
    with patch("trading_bot.backup_comms.env_get") as eg:
        def _eg(k):
            return "+15551234567" if k == "SMS_TO_NUMBER" else ""
        eg.side_effect = _eg
        ready, cfg = twilio_config()
        assert ready is False
        assert cfg["to"] == "+15551234567"
        assert notify_sms("BUY test") is False


def test_resolve_backup_from_newguy_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NEWGUY_TELEGRAM_BOT_TOKEN", raising=False)
    ng = tmp_path / ".env"
    ng.write_text("TELEGRAM_BOT_TOKEN=999999:AA-fake-newguy-token-for-test\n")
    with patch("trading_bot.backup_comms.NEWGUY_ENV_PATH", ng), patch(
        "trading_bot.backup_comms.env_get", return_value=""
    ):
        token, chat, source = resolve_backup_telegram()
    assert token.startswith("999999:")
    assert source == "newguy_env"
    assert chat  # default chat


@pytest.mark.asyncio
async def test_notifier_mirrors_critical_only():
    n = Notifier(telegram=MagicMock())
    n.telegram.send_message = AsyncMock()
    n.backup_enabled = True
    n.backup_token = "1:tok"
    n.backup_chat_id = "123"
    with patch(
        "trading_bot.notifier.send_telegram_message", new_callable=AsyncMock
    ) as send_b, patch("trading_bot.notifier.notify_sms", return_value=False):
        await n.send("heartbeat ok nothing critical")
        send_b.assert_not_called()
        await n.send("🟢 BUY BTC-USD $100 @ 1.0 (score 70)")
        send_b.assert_awaited()
        args = send_b.await_args[0]
        assert args[0] == "1:tok"
        assert "BUY" in args[2]
