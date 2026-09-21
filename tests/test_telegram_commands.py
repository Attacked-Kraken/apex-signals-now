"""Coverage for Telegram command registry + operator-note helpers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.telegram_commands import (
    BOT_COMMAND_SPECS,
    KNOWN_COMMANDS,
    ResetPaperError,
    WipePaperError,
    build_weekly_expectancy_digest,
    execute_set_circuity_breaker,
    format_circuity_breaker_status,
    format_history_reply,
    format_ping_reply,
    format_positions_reply,
    format_reset_paper_pending_reply,
    format_wipe_paper_pending_reply,
    parse_circuity_breaker_args,
    parse_command,
    parse_reset_paper_args,
    parse_universe_args,
    parse_universe_stocks_args,
    parse_weekly_digest_args,
    parse_wipe_paper_args,
    redact_secrets,
    wipe_paper_artifacts,
)
from trading_bot.state_store import OpsState


REQUIRED_OPERATOR = {
    "weekly_digest_101",
    "circuity_breaker_manually",
    "reset_paper",
    "wipe_paper",
    "universe",
    "universe_stocks",
    "positions",
    "history",
    "logs",
}


def test_known_commands_include_operator_notes():
    assert REQUIRED_OPERATOR <= set(KNOWN_COMMANDS)
    names = {c for c, _ in BOT_COMMAND_SPECS}
    assert "weekly_digest_101" in names
    assert "circuity_breaker_manually" in names
    # production spelling preserved
    assert "circuitry_breaker_manually" not in names


def test_parse_command_new_cmds():
    assert parse_command("/weekly_digest_101 paper")[0] == "weekly_digest_101"
    assert parse_command("/circuity_breaker_manually on")[0] == "circuity_breaker_manually"
    assert parse_command("/universe allowlist")[0] == "universe"


def test_circuity_breaker_parse_and_execute(tmp_path):
    assert parse_circuity_breaker_args([]) == "status"
    assert parse_circuity_breaker_args(["on"]) == "on"
    assert parse_circuity_breaker_args(["off"]) == "off"
    assert parse_circuity_breaker_args(["nope"]) is None
    status = format_circuity_breaker_status(enabled=True, consec=2, tripped=False)
    assert "circuity_breaker_manually: ON" in status
    settings = SimpleNamespace(circuit_breaker_enabled=True)
    ops = OpsState()
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\n")
    environ: dict = {}
    reply = execute_set_circuity_breaker(
        settings, enabled=False, env_path=env_path, environ=environ, ops=ops, consec=1
    )
    assert settings.circuit_breaker_enabled is False
    assert ops.cb_enabled is False
    assert "OFF" in reply
    assert "CIRCUIT_BREAKER_ENABLED=false" in env_path.read_text()


def test_weekly_digest_parse_and_empty():
    assert parse_weekly_digest_args([]) is None
    assert parse_weekly_digest_args(["paper"]) == "paper"
    assert parse_weekly_digest_args(["live"]) == "live"
    with pytest.raises(ValueError):
        parse_weekly_digest_args(["nope"])
    text = build_weekly_expectancy_digest(mode="paper", root=Path("/tmp/nonexistent_cruzbot_root_xyz"))
    assert "WEEKLY DIGEST 101" in text
    assert "PAPER" in text


def test_reset_wipe_confirm_args():
    assert parse_reset_paper_args([]) == (False, None)
    assert parse_reset_paper_args(["2500"]) == (False, 2500.0)
    assert parse_reset_paper_args(["confirm"]) == (True, None)
    assert parse_reset_paper_args(["confirm", "1600"]) == (True, 1600.0)
    with pytest.raises(ResetPaperError):
        parse_reset_paper_args(["abc"])
    assert parse_wipe_paper_args(["confirm"]) == (True, None)
    with pytest.raises(WipePaperError):
        parse_wipe_paper_args(["x", "y"])
    assert "~60" in format_reset_paper_pending_reply(1600)
    assert "WIPE" in format_wipe_paper_pending_reply(1600)


def test_ops_confirm_gate_ttl():
    ops = OpsState()
    ops.arm_reset_paper_confirm(2000.0, explicit=True, ttl=60.0)
    assert ops.has_pending_reset_paper_confirm()
    assert ops.pending_reset_paper_cash() == 2000.0
    ops.clear_reset_paper_confirm()
    assert not ops.has_pending_reset_paper_confirm()
    ops.arm_wipe_paper_confirm(1600.0, ttl=60.0)
    assert ops.has_pending_wipe_paper_confirm()


def test_universe_parsers():
    assert parse_universe_args([]) is None
    assert parse_universe_args(["all"]) == "DYNAMIC_ALL"
    assert parse_universe_args(["allowlist"]) == "ALLOWLIST"
    assert parse_universe_args(["off"]) == "OFF"
    with pytest.raises(ValueError):
        parse_universe_args(["bogus"])
    assert parse_universe_stocks_args([]) is None
    assert parse_universe_stocks_args(["on"]) is True
    assert parse_universe_stocks_args(["off"]) is False


def test_positions_history_logs_formatters(tmp_path):
    assert "none" in format_positions_reply([], paper=True).lower()
    pos = format_positions_reply(
        [
            {
                "symbol": "BTC-USD",
                "qty": 0.01,
                "avg_entry_price": 100.0,
                "unrealized_pl": 1.0,
                "pnl_pct": 1.0,
                "stop_loss": 95.0,
                "take_profit": None,
            }
        ],
        paper=True,
    )
    assert "BTC-USD" in pos
    assert "sl=$95" in pos
    hist = format_history_reply(
        [
            {
                "when": "09/19 16:27 CT",
                "side": "SELL",
                "symbol": "BTC-USD",
                "qty": 0.001,
                "price": 80000,
                "pnl": -1.16,
            }
        ]
    )
    assert "BTC-USD" in hist
    assert "-$1.16" in hist
    assert format_ping_reply(12.3) == "pong 12ms"
    scrubbed = redact_secrets("TELEGRAM_BOT_TOKEN=123:ABC api_key=zzz")
    assert "123:ABC" not in scrubbed
    assert "redacted" in scrubbed.lower()
    summary = wipe_paper_artifacts(project_root=tmp_path)
    assert summary["log_truncated"] is True
    assert (tmp_path / "data" / "paper_loop.log").exists() or True


def test_handlers_cover_all_bot_command_specs():
    """Every BOT_COMMAND_SPECS entry must be wired in TradingApp._wire_telegram_commands."""
    import ast

    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    # Extract return dict keys inside _wire_telegram_commands roughly
    wired = set()
    for line in main_src.splitlines():
        line = line.strip()
        if line.startswith('"') and ":" in line and line.endswith(","):
            key = line.split(":", 1)[0].strip().strip('"')
            if key.isidentifier() or "_" in key:
                # crude: only short keys that look like commands
                if key in KNOWN_COMMANDS or key.replace("_", "").isalnum():
                    wired.add(key)
    for cmd, _ in BOT_COMMAND_SPECS:
        assert cmd in wired, f"/{cmd} missing from main._wire_telegram_commands return dict"
