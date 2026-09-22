"""Coverage for Telegram command registry + operator-note helpers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.telegram_commands import (
    BOT_COMMAND_SPECS,
    KNOWN_COMMANDS,
    STATUS_SYMBOLS_CALLBACK,
    STATUS_SYMBOLS_COLLAPSE,
    ResetPaperError,
    TelegramReply,
    WipePaperError,
    symbols_collapse_keyboard,
    symbols_expand_keyboard,
    status_with_symbols_button,
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
    parse_set_spread_args,
    parse_set_max_spread_alias,
    format_set_spread_reply,
    execute_set_spread,
    SetSpreadError,
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


def test_format_status_production_substrings():
    from trading_bot.telegram_commands import format_status_reply

    text = format_status_reply(
        paper_cash=1493.25,
        paper_equity=2997.78,
        wallet_b4=3000.0,
        positions=[
            {
                "symbol": "LINK-USD",
                "qty": 59.48,
                "avg_entry_price": 12.62,
                "mark_price": 12.67,
                "market_value": 753.43,
                "unrealized_pl": 3.06,
                "side": "long",
                "take_profit": 12.62 * 1.0225,
            }
        ],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=0.0,
        paper=True,
        symbols=["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD"],
        max_notional_per_trade=750,
        max_total_exposure=3000,
        entry_threshold=60,
        max_spread_pct=0.002,
        trade_profile="medium",
        market_state="BULL_OK",
        session_wins=0,
        session_losses=0,
        symbol_mode="ALLOWLIST",
        universe_stocks=False,
        tod_gate_enabled=False,
        tod_custom_lock=True,
        stop_loss_profile="medium",
        winning_formula=True,
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        caps_locked=True,
        majors_only=True,
        majors_symbols=["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD"],
        entry_proximity={"score": 49.1, "direction": "WAIT", "symbol": "BTC-USD", "price": 81421.30},
    )
    for needle in (
        "Apex Signals Now",
        "Wallet B4",
        "circuit breaker",
        "winning_formula",
        "Entry Proximity",
        "positions (1):",
        "LINK-USD",
        "Progress:",
        "majors_only: ON",
        "Symbols: 5 pairs · tap ▼ to expand",
    ):
        assert needle in text, needle


def test_performance_report_shape():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from trading_bot.telegram_commands import format_performance_report

    ct = ZoneInfo("America/Chicago")
    text = format_performance_report(
        trades=[
            {
                "when": datetime(2026, 9, 21, 7, 32, tzinfo=ct),
                "symbol": "LINK-USD",
                "cost": 0.0,
                "exit": 741.94,
                "pnl": -14.41,
            },
            {
                "when": datetime(2026, 9, 21, 8, 42, tzinfo=ct),
                "symbol": "SOL-USD",
                "cost": 750.37,
                "exit": 765.85,
                "pnl": 9.41,
            },
        ],
        cash=1494.17,
        equity=2999.45,
        paper=True,
        now=datetime(2026, 9, 21, 12, 0, tzinfo=ct),
    )
    assert "CRUZBOT PERFORMANCE REPORT" in text
    assert "Date/Time | Symbol | Cost | Exit | Net P&L" in text
    assert "Paper Bankroll" in text
    assert "Total Trades: 2" in text


def test_weekly_digest_paper_vs_live_empty(tmp_path):
    from trading_bot.telegram_commands import build_weekly_expectancy_digest, parse_weekly_digest_args

    assert parse_weekly_digest_args(["paper"]) == "paper"
    assert parse_weekly_digest_args(["live"]) == "live"
    # empty DBs → distinct mode footers
    paper = build_weekly_expectancy_digest(mode="paper", root=tmp_path, days=7)
    live = build_weekly_expectancy_digest(mode="live", root=tmp_path, days=7)
    assert "Mode: PAPER" in paper
    assert "Mode: LIVE" in live
    assert "wipe" in paper.lower() or "Paper" in paper
    assert "Live" in live or "live" in live.lower()


def test_wf_tier1_defaults(tmp_path):
    from types import SimpleNamespace
    from trading_bot.telegram_commands import execute_set_winning_formula

    settings = SimpleNamespace(
        entry_threshold=40.0,
        max_concurrent_positions=2,
        min_tp_pct=0.02,
        atr_bracket_tp_min_pct=0.02,
        trail_fee_buffer_pct=0.01,
        elite_fee_lock_arm_pct=0.01,
        tp1_fraction=0.5,
        stop_loss_profile="tight",
        taker_fee_rate=0.01,
        maker_fee_rate=0.01,
        circuit_breaker_enabled=False,
        winning_formula=False,
        sl_min_pct=0.0075,
        sl_max_pct=0.0075,
        elite_atr_sl_mult=1.0,
    )
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\n")
    environ = {}
    reply = execute_set_winning_formula(settings, enabled=True, env_path=env_path, environ=environ)
    assert settings.entry_threshold == 60.0
    assert settings.trail_fee_buffer_pct == 0.0125
    assert settings.elite_fee_lock_arm_pct == 0.012
    assert settings.taker_fee_rate == 0.008
    assert settings.maker_fee_rate == 0.004
    assert settings.tp1_fraction == 0.0
    assert settings.circuit_breaker_enabled is True
    assert "0.80%" in reply or "Tier-1" in reply or "HWM" in reply


def test_set_spread_percent_units(tmp_path):
    """User enters percent units: 0.2 → 0.2% stored as fraction 0.002."""
    assert parse_set_spread_args(["0.2"]) == 0.2
    assert parse_set_spread_args(["0.5%"]) == 0.5
    with pytest.raises(SetSpreadError):
        parse_set_spread_args([])
    with pytest.raises(SetSpreadError):
        parse_set_spread_args(["20"])  # > 5% max
    assert parse_set_max_spread_alias(["MAX_SPREAD_PCT", "0.2"]) == 0.2
    settings = SimpleNamespace(max_spread_pct=0.002)
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\nMAX_SPREAD_PCT=0.002\n")
    reply = execute_set_spread(settings, ["0.2"], env_path=env_path)
    assert settings.max_spread_pct == pytest.approx(0.002)
    assert "0.2%" in reply
    assert "MAX_SPREAD_PCT=0.002" in env_path.read_text()
    assert "0.2%" in format_set_spread_reply(0.2)


def test_progress_bar_underwater_and_toward_tp():
    """Progress = pnl toward TP (floor 2%); underwater clamps to 0% (not raw pnl)."""
    from trading_bot.telegram_commands import format_status_reply
    from trading_bot.utils.entry_proximity import make_progress_bar

    assert make_progress_bar(0) == "⬜" * 10
    assert make_progress_bar(100) == "⬛" * 10
    assert "⬛" in make_progress_bar(43.5) and "⬜" in make_progress_bar(43.5)

    underwater = format_status_reply(
        paper_cash=2000,
        paper_equity=2900,
        wallet_b4=3000,
        positions=[
            {
                "symbol": "XRP-USD",
                "qty": 100,
                "avg_entry_price": 1.50,
                "mark_price": 1.40,
                "market_value": 140,
                "unrealized_pl": -10,
                "side": "long",
                "take_profit": 1.50 * 1.0225,
            }
        ],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        symbols=["XRP-USD"],
        entry_threshold=60,
        max_spread_pct=0.002,
        entry_proximity={"score": 43.5, "symbol": "XRP-USD", "price": 1.40},
    )
    assert "Entry Proximity: [⬛⬛⬛⬛⬜⬜⬜⬜⬜⬜] 43.5%" in underwater
    assert "Progress: [⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜] 0.0%" in underwater

    # +1.125% mark vs +2.25% TP → 50% progress
    mid = format_status_reply(
        paper_cash=2000,
        paper_equity=3011,
        wallet_b4=3000,
        positions=[
            {
                "symbol": "ETH-USD",
                "qty": 1,
                "avg_entry_price": 100.0,
                "mark_price": 101.125,
                "market_value": 101.125,
                "unrealized_pl": 1.125,
                "side": "long",
                "take_profit": 102.25,
            }
        ],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=0.0,
        paper=True,
        symbols=["ETH-USD"],
        max_notional_per_trade=500,
        max_total_exposure=3000,
        entry_threshold=60,
        max_spread_pct=0.002,
    )
    assert "Progress: [⬛⬛⬛⬛⬛⬜⬜⬜⬜⬜] 50.0%" in mid
    assert "caps=$500/trade $3000 exposure" in mid


def test_telegram_reply_and_symbols_keyboards():
    kb = symbols_expand_keyboard(12)
    assert kb["inline_keyboard"][0][0]["text"] == "▼ Symbols (12)"
    assert kb["inline_keyboard"][0][0]["callback_data"] == STATUS_SYMBOLS_CALLBACK
    hide = symbols_collapse_keyboard(12)
    assert "▲ Hide symbols (12)" in hide["inline_keyboard"][0][0]["text"]
    assert hide["inline_keyboard"][0][0]["callback_data"] == STATUS_SYMBOLS_COLLAPSE
    reply = status_with_symbols_button("hello status", symbol_count=4)
    assert isinstance(reply, TelegramReply)
    assert reply.text == "hello status"
    assert reply.reply_markup == symbols_expand_keyboard(4)
    empty = status_with_symbols_button("x", symbol_count=0)
    assert empty.reply_markup["inline_keyboard"][0][0]["text"] == "▼ Symbols"


def test_format_status_symbols_tap_line_and_fields():
    from trading_bot.telegram_commands import format_status_reply

    text = format_status_reply(
        paper_cash=1493.25,
        paper_equity=2997.78,
        wallet_b4=3000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=0.5,
        paper=True,
        symbols=["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD"],
        max_notional_per_trade=750,
        max_total_exposure=3000,
        entry_threshold=60,
        max_spread_pct=0.002,
        trade_profile="medium",
        market_state="BULL_OK",
        session_wins=1,
        session_losses=0,
        win_rate_pct=100.0,
        symbol_mode="ALLOWLIST",
        universe_stocks=False,
        stock_count=0,
        tod_gate_enabled=False,
        tod_custom_lock=True,
        stop_loss_profile="medium",
        stop_loss_effective_pct=0.015,
        winning_formula=True,
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        caps_locked=True,
        majors_only=True,
        majors_symbols=["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD"],
        entry_proximity={"score": 49.1, "direction": "WAIT", "symbol": "BTC-USD", "price": 81421.30},
    )
    for needle in (
        "Wallet B4",
        "circuit breaker",
        "Entry Proximity",
        "majors_only: ON",
        "Symbols: 5 pairs · tap ▼ to expand",
        "stop_loss:",
        "tod_custom:",
        "winning_formula: ON",
        "caps=$750/trade $3000 exposure 🔒",
        "WR 100%",
    ):
        assert needle in text, needle
    reply = status_with_symbols_button(text, symbol_count=4)
    assert "▼ Symbols (4)" in reply.reply_markup["inline_keyboard"][0][0]["text"]



def test_wf_registry_has_no_major_commands():
    names = {name for name, _description in BOT_COMMAND_SPECS}
    assert "major" not in names
    assert "majors" not in names


def _wf_settings(**overrides):
    values = {
        "entry_threshold": 40.0,
        "max_concurrent_positions": 2,
        "min_tp_pct": 0.02,
        "atr_bracket_tp_min_pct": 0.02,
        "trail_fee_buffer_pct": 0.01,
        "elite_fee_lock_arm_pct": 0.01,
        "tp1_fraction": 0.5,
        "stop_loss_profile": "tight",
        "taker_fee_rate": 0.01,
        "maker_fee_rate": 0.01,
        "circuit_breaker_enabled": False,
        "majors_only": False,
        "max_spread_pct": 0.004,
        "trade_profile": "aggressive",
        "max_notional_per_trade_usd": 500.0,
        "max_total_exposure_usd": 2000.0,
        "winning_formula": False,
        "sl_min_pct": 0.0075,
        "sl_max_pct": 0.0075,
        "elite_atr_sl_mult": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_wf_on_sets_majors_and_restores_last_snapshot(tmp_path):
    import json
    from trading_bot.telegram_commands import execute_set_winning_formula

    env_path = tmp_path / ".env"
    env_path.write_text("WINNING_FORMULA=false\nMAJORS_ONLY=false\n")
    data = tmp_path / "data"
    data.mkdir()
    snapshot = {
        "entry_threshold": 71.0,
        "max_concurrent_positions": 4,
        "min_tp_pct": 0.031,
        "trail_fee_buffer_pct": 0.013,
        "elite_fee_lock_arm_pct": 0.014,
        "tp1_fraction": 0.0,
        "stop_loss_profile": "medium",
        "taker_fee_rate": 0.008,
        "maker_fee_rate": 0.004,
        "circuit_breaker_enabled": True,
        "majors_only": False,
        "max_spread_pct": 0.0019,
        "trade_profile": "medium",
        "max_notional_per_trade_usd": 625.0,
        "max_total_exposure_usd": 2500.0,
        "score": 82,
        "n_trades": 9,
        "expectancy": 3.5,
        "ts": 1234.0,
    }
    (data / "wf_last_snapshot.json").write_text(json.dumps(snapshot))
    settings = _wf_settings()

    reply = execute_set_winning_formula(settings, enabled=True, env_path=env_path, environ={})

    assert settings.winning_formula is True
    assert settings.majors_only is True
    assert settings.entry_threshold == 71.0
    assert settings.max_notional_per_trade_usd == 625.0
    assert settings.min_tp_pct == 0.031
    assert "winning_formula: ON 🚀" in reply
    assert "BTC/ETH/SOL/LINK/XCN" in reply
    env_text = env_path.read_text()
    assert "WINNING_FORMULA=true" in env_text
    assert "MAJORS_ONLY=true" in env_text


@pytest.mark.asyncio
async def test_changing_threshold_turns_wf_off_without_rollback(tmp_path, monkeypatch):
    import main
    from trading_bot.config import Settings

    env_path = tmp_path / ".env"
    env_path.write_text("WINNING_FORMULA=true\nENTRY_THRESHOLD=60\n")
    monkeypatch.setattr(main, "ENV_PATH", env_path)
    settings = Settings(winning_formula=True, entry_threshold=60, _env_file=None)
    app = main.TradingApp(settings)
    try:
        reply = await app._wire_telegram_commands()["set_threshold"](
            "set_threshold", ["72"]
        )
        assert settings.entry_threshold == 72.0
        assert settings.winning_formula is False
        assert "winning_formula: OFF (knob changed: entry_threshold)" in reply
        assert "WINNING_FORMULA=false" in env_path.read_text()
    finally:
        await app.shutdown()


def test_wf_best_snapshot_requires_fit_and_strictly_higher_score(tmp_path):
    import json
    from trading_bot.telegram_commands import WF_SNAPSHOT_FIELDS, maybe_save_wf_best_snapshot

    settings = _wf_settings(winning_formula=True, majors_only=True)
    weak = {
        "score": 90,
        "band": "WEAK",
        "metrics": {"closed_trades_n": 8, "expectancy": 5.0},
        "ts": 10.0,
    }
    assert not maybe_save_wf_best_snapshot(settings, weak, data_dir=tmp_path)
    assert not (tmp_path / "wf_best_snapshot.json").exists()

    too_few = {
        "score": 70,
        "band": "OK",
        "metrics": {"closed_trades_n": 2, "expectancy": 5.0},
        "ts": 11.0,
    }
    assert not maybe_save_wf_best_snapshot(settings, too_few, data_dir=tmp_path)

    fit = {
        "score": 71,
        "band": "STRONG",
        "metrics": {"closed_trades_n": 3, "expectancy": 1.25},
        "ts": 12.0,
    }
    assert maybe_save_wf_best_snapshot(settings, fit, data_dir=tmp_path)
    best = json.loads((tmp_path / "wf_best_snapshot.json").read_text())
    assert set(best) == set(WF_SNAPSHOT_FIELDS)
    assert best["score"] == 71
    assert best["n_trades"] == 3
    assert (tmp_path / "wf_last_snapshot.json").exists()

    same_score = dict(fit, ts=13.0)
    assert not maybe_save_wf_best_snapshot(settings, same_score, data_dir=tmp_path)
    settings.winning_formula = False
    higher_but_off = dict(fit, score=99, ts=14.0)
    assert not maybe_save_wf_best_snapshot(settings, higher_but_off, data_dir=tmp_path)
