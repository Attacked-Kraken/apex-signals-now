"""CB auto-resume: timer expiry is the decision (no win/regime gate)."""
from __future__ import annotations

import time
from pathlib import Path

from trading_bot.state_store import OpsState


def test_cb_auto_resume_ready_on_timer_expiry_without_win_or_bull(tmp_path: Path):
    ops = OpsState(cb_state_path=tmp_path / "cb_auto_resume.json")
    ops.arm_cb_auto_resume(cooldown_sec=60.0)
    assert ops.paused is True
    assert ops.cb_auto_resume_armed is True
    # Force wall deadline into the past.
    ops.cb_auto_resume_at_wall = time.time() - 1.0
    ops.cb_auto_resume_at = time.monotonic() - 1.0
    ops.cb_win_since_trip = False
    assert ops.cb_auto_resume_remaining_seconds() == 0.0
    # Timer expiry alone — regime_bull_ok=False must still be ready.
    assert ops.cb_auto_resume_ready(regime_bull_ok=False) is True


def test_cb_auto_resume_not_ready_while_cooldown_active(tmp_path: Path):
    ops = OpsState(cb_state_path=tmp_path / "cb_auto_resume.json")
    ops.arm_cb_auto_resume(cooldown_sec=600.0)
    assert ops.cb_auto_resume_remaining_seconds() > 0.0
    assert ops.cb_auto_resume_ready(regime_bull_ok=True) is False


def test_arm_cb_clears_phd_weak_bypass(tmp_path: Path):
    ops = OpsState(cb_state_path=tmp_path / "cb_auto_resume.json")
    ops.extra["phd_weak_bypass_after_cb"] = True
    ops.arm_cb_auto_resume(cooldown_sec=60.0)
    assert "phd_weak_bypass_after_cb" not in ops.extra


def test_phd_weak_bypass_persists_across_restore(tmp_path: Path):
    """After CB auto-resume, bypass must survive process restart via sibling file."""
    cb_path = tmp_path / "cb_auto_resume.json"
    bypass_path = tmp_path / "cb_phd_bypass.json"
    ops = OpsState(cb_state_path=cb_path, phd_bypass_path=bypass_path)
    ops.set_phd_weak_bypass_after_cb(True)
    assert ops.extra.get("phd_weak_bypass_after_cb") is True
    assert bypass_path.exists()
    payload = bypass_path.read_text(encoding="utf-8")
    assert "phd_weak_bypass_after_cb" in payload

    # Simulate fresh process: new OpsState, restore from disk.
    ops2 = OpsState(cb_state_path=cb_path, phd_bypass_path=bypass_path)
    assert "phd_weak_bypass_after_cb" not in ops2.extra
    assert ops2.restore_phd_weak_bypass_from_disk() is True
    assert ops2.extra.get("phd_weak_bypass_after_cb") is True


def test_arm_cb_clears_persisted_phd_weak_bypass(tmp_path: Path):
    cb_path = tmp_path / "cb_auto_resume.json"
    bypass_path = tmp_path / "cb_phd_bypass.json"
    ops = OpsState(cb_state_path=cb_path, phd_bypass_path=bypass_path)
    ops.set_phd_weak_bypass_after_cb(True)
    assert bypass_path.exists()
    ops.arm_cb_auto_resume(cooldown_sec=60.0)
    assert "phd_weak_bypass_after_cb" not in ops.extra
    assert not bypass_path.exists()


def test_status_cb_line_shows_resume_and_bypass():
    from trading_bot.telegram_commands import format_status_reply

    paused_txt = format_status_reply(
        paper_cash=1000.0,
        paper_equity=1000.0,
        positions=[],
        paused=True,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=3,
        circuit_breaker_resume_seconds=12 * 60.0,
        circuit_breaker_paused=True,
    )
    assert "paused · resumes in 12m" in paused_txt

    bypass_txt = format_status_reply(
        paper_cash=1000.0,
        paper_equity=1000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        circuit_breaker_paused=False,
        phd_weak_bypass_after_cb=True,
    )
    assert "auto-resumed · WEAK bypass" in bypass_txt
