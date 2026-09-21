"""PHD mode (Professional High Discipline) — ops pack + WEAK soft entry gate."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.telegram_commands import (
    execute_set_phd_mode,
    format_phd_status_line,
    format_status_reply,
    parse_phd_args,
    phd_weak_entry_gate,
)
from trading_bot.state_store import OpsState


def test_parse_phd_args():
    assert parse_phd_args([]) == "status"
    assert parse_phd_args(["on"]) == "on"
    assert parse_phd_args(["OFF"]) == "off"
    assert parse_phd_args(["status"]) == "status"
    assert parse_phd_args(["nope"]) is None


def test_phd_on_applies_winning_formula_majors_and_flag(tmp_path):
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
        trade_profile="aggressive",
        majors_only=False,
        phd_mode=False,
        sl_min_pct=0.0075,
        sl_max_pct=0.0075,
        elite_atr_sl_mult=1.0,
    )
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\n")
    environ: dict = {}
    ops = OpsState(cb_enabled=False)
    reply = execute_set_phd_mode(
        settings,
        enabled=True,
        env_path=env_path,
        environ=environ,
        ops=ops,
        majors_symbols=["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD"],
    )
    assert settings.phd_mode is True
    assert settings.winning_formula is True
    assert settings.majors_only is True
    assert settings.trade_profile == "medium"
    assert settings.stop_loss_profile == "medium"
    assert settings.circuit_breaker_enabled is True
    assert ops.cb_enabled is True
    assert "PHD mode ON" in reply
    assert "winning_formula: ON" in reply
    env_txt = env_path.read_text()
    assert "PHD_MODE=true" in env_txt
    assert "WINNING_FORMULA=true" in env_txt or environ.get("WINNING_FORMULA") == "true"
    assert environ.get("PHD_MODE") == "true"
    assert "MAJORS_ONLY=true" in env_txt or environ.get("MAJORS_ONLY") == "true" or settings.majors_only


def test_phd_off_leaves_winning_formula(tmp_path):
    settings = SimpleNamespace(
        winning_formula=True,
        phd_mode=True,
        trade_profile="medium",
        majors_only=True,
        circuit_breaker_enabled=True,
        stop_loss_profile="medium",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("PHD_MODE=true\nWINNING_FORMULA=true\n")
    ops = OpsState()
    ops.focus_blocked = "phd: formula WEAK (12/100) — see /formula"
    reply = execute_set_phd_mode(settings, enabled=False, env_path=env_path, ops=ops)
    assert settings.phd_mode is False
    assert settings.winning_formula is True  # left as-is
    assert ops.focus_blocked == ""
    assert "left as-is" in reply
    assert "PHD_MODE=false" in env_path.read_text()


def test_phd_weak_gate_blocks_entries_only():
    assert phd_weak_entry_gate(phd_mode=False, formula_band="WEAK", formula_score=10) is None
    assert phd_weak_entry_gate(phd_mode=True, formula_band="STRONG", formula_score=80) is None
    assert phd_weak_entry_gate(phd_mode=True, formula_band="OK", formula_score=50) is None
    reason = phd_weak_entry_gate(phd_mode=True, formula_band="WEAK", formula_score=22)
    assert reason == "phd: formula WEAK (22/100) — see /formula"
    assert "see /formula" in reason


def test_format_status_includes_phd_line():
    text = format_status_reply(
        paper_cash=1500.0,
        paper_equity=3000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        symbols=["BTC-USD"],
        entry_threshold=60,
        max_spread_pct=0.002,
        api_risk_line="api_risk: 🟢 LOW (12/100)",
        formula_score_line="formula: 🟢 78",
        phd_mode=True,
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        winning_formula=True,
        trade_profile="medium",
        universe_stocks=False,
        symbol_mode="ALLOWLIST",
    )
    assert "formula: 🟢 78" in text
    assert "phd: ON" in text
    form_i = text.index("formula:")
    phd_i = text.index("phd: ON")
    cb_i = text.index("circuit breaker")
    assert form_i < phd_i < cb_i

    off = format_status_reply(
        paper_cash=1500.0,
        paper_equity=3000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        symbols=["BTC-USD"],
        formula_score_line="formula: 🟡 50",
        phd_mode=False,
        circuit_breaker_on=True,
        winning_formula=True,
        trade_profile="medium",
    )
    assert "phd: OFF" in off
    assert format_phd_status_line(enabled=True) == "phd: ON"
