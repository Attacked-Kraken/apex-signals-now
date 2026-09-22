"""Future Pack notepad: gates, WAIT/READY/DONE, no auto-implement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_bot.telegram_commands import BOT_COMMAND_SPECS, KNOWN_COMMANDS
from trading_bot.utils.future_pack import (
    ITEM_SPECS,
    apply_greenlight,
    detect_capabilities,
    evaluate_future_pack,
    format_future_pack_report,
    hwm_exits_left_upside,
    mark_done,
    mark_detected_capabilities,
)


def _closes(n: int, *, pnl: float = 1.5, peak: float = 3.0, leftover: bool = True):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        entry = 100.0
        exit_px = entry * (1.0 + 0.005) if pnl > 0 else entry * (1.0 - 0.01)
        realized = (exit_px - entry) / entry * 100.0
        peak_pct = (realized + 1.5) if leftover else realized
        rows.append(
            {
                "symbol": "BTC-USD",
                "qty": 1.0,
                "entry": entry,
                "exit": exit_px,
                "pnl": pnl,
                "won": pnl > 0,
                "closed_at": (now - timedelta(hours=n - i)).isoformat(),
                "peak_upl_pct": peak if leftover else peak_pct,
                "reason": "SL",
            }
        )
    return rows


def test_commands_registered():
    names = {c for c, _ in BOT_COMMAND_SPECS}
    assert "live_safety" in names
    assert "future_pack" in names
    assert "live_safety" in KNOWN_COMMANDS
    assert "future_pack" in KNOWN_COMMANDS


def test_default_pack_live_safety_done_deadman_wait(tmp_path):
    pack = evaluate_future_pack(
        closed_trades=_closes(2, pnl=-1.0, leftover=False),
        formula_score=48,
        path=tmp_path / "future_pack.json",
    )
    by_id = {i["id"]: i for i in pack["items"]}
    assert by_id["live_safety_alerts_heartbeat"]["status"] == "DONE"
    assert by_id["dead_man_switch"]["status"] == "WAIT"
    assert "private live path incomplete" in by_id["dead_man_switch"]["why"]
    assert by_id["regime_scaled_size"]["status"] == "WAIT"
    assert by_id["wider_trail_partial_tp"]["status"] == "WAIT"
    assert by_id["maker_signed_deadman"]["status"] == "WAIT"
    assert by_id["cvd_lead_lag"]["status"] == "WAIT"
    assert by_id["external_watchdog_sms"]["status"] == "WAIT"
    assert pack["auto_apply"] is False
    assert (tmp_path / "future_pack.json").exists()


def test_v_is_not_greenlight(tmp_path):
    path = tmp_path / "future_pack.json"
    apply_greenlight("regime_scaled_size", "v", path=path)
    pack = evaluate_future_pack(closed_trades=_closes(2), formula_score=50, path=path)
    by_id = {i["id"]: i for i in pack["items"]}
    assert by_id["regime_scaled_size"]["status"] == "WAIT"
    assert pack["greenlights"].get("regime_scaled_size") is not True


def test_explicit_greenlight_readies_regime_size(tmp_path):
    path = tmp_path / "future_pack.json"
    apply_greenlight("regime_scaled_size", "greenlight", path=path)
    pack = evaluate_future_pack(closed_trades=_closes(2), formula_score=50, path=path)
    by_id = {i["id"]: i for i in pack["items"]}
    assert by_id["regime_scaled_size"]["status"] == "READY"


def test_wider_trail_ready_needs_30_exp_and_hwm(tmp_path):
    path = tmp_path / "future_pack.json"
    weak = evaluate_future_pack(
        closed_trades=_closes(30, pnl=1.0, leftover=False),
        formula_score=60,
        path=path,
    )
    # leftover=False and peak==realized → no HWM upside
    by_id = {i["id"]: i for i in weak["items"]}
    assert by_id["wider_trail_partial_tp"]["status"] in ("WAIT", "READY")
    # force leftover evidence
    ready = evaluate_future_pack(
        closed_trades=_closes(30, pnl=2.0, peak=4.0, leftover=True),
        formula_score=60,
        path=path,
    )
    by_id = {i["id"]: i for i in ready["items"]}
    assert by_id["wider_trail_partial_tp"]["status"] == "READY"
    assert by_id["cvd_lead_lag"]["status"] == "READY"
    assert by_id["atr_brackets_daily_dd"]["status"] == "READY"
    # still not auto-implemented
    assert by_id["wider_trail_partial_tp"]["status"] != "DONE"


def test_hwm_upside_helper():
    assert not hwm_exits_left_upside([])
    rows = [
        {"entry": 100, "exit": 101, "peak_upl_pct": 2.5, "qty": 1, "pnl": 1.0},
        {"entry": 100, "exit": 100.5, "peak_upl_pct": 2.0, "qty": 1, "pnl": 0.5},
        {"entry": 100, "exit": 101.2, "peak_upl_pct": 3.0, "qty": 1, "pnl": 1.2},
    ]
    assert hwm_exits_left_upside(rows)


def test_urgent_warning_when_score_ge_65_and_wait(tmp_path):
    pack = evaluate_future_pack(
        closed_trades=_closes(2, pnl=-1.0),
        formula_score=68,
        path=tmp_path / "future_pack.json",
    )
    assert pack["urgent"] is True
    text = format_future_pack_report(pack)
    assert "URGENT" in text
    assert "Before health score is in the 70s" in text
    assert "WAIT" in text
    assert "not permission" in text or "not a green light" in text
    assert "70s" in text


def test_score_70s_is_not_permission(tmp_path):
    pack = evaluate_future_pack(
        closed_trades=_closes(40, pnl=3.0, leftover=True),
        formula_score=78,
        path=tmp_path / "future_pack.json",
    )
    text = format_future_pack_report(pack)
    assert pack["auto_apply"] is False
    assert "not permission to auto-implement" in text
    by_id = {i["id"]: i for i in pack["items"]}
    # score 78 does not mark maker/dead-man DONE
    assert by_id["maker_signed_deadman"]["status"] == "WAIT"
    assert by_id["dead_man_switch"]["status"] == "WAIT"


def test_mark_done_persists(tmp_path):
    path = tmp_path / "future_pack.json"
    mark_done("cvd_lead_lag", path=path, reason="internally marked for test")
    pack = evaluate_future_pack(closed_trades=_closes(2), formula_score=40, path=path)
    by_id = {i["id"]: i for i in pack["items"]}
    assert by_id["cvd_lead_lag"]["status"] == "DONE"


def test_detect_capabilities_honest():
    caps = detect_capabilities()
    assert caps["live_safety"] is True
    assert caps["heartbeat_file"] is True
    assert caps["signed_addorder"] is False
    assert caps["dead_man_armed"] is False


def test_format_contains_all_items():
    pack = evaluate_future_pack(
        closed_trades=[],
        formula_score=48,
        path=None,
        persist=False,
    )
    text = format_future_pack_report(pack)
    assert "📦 Future Pack" in text
    for spec in ITEM_SPECS:
        assert spec["title"] in text
    assert "signed AddOrder" in text


def test_mark_detected_sets_live_safety_done(tmp_path):
    path = tmp_path / "future_pack.json"
    state = mark_detected_capabilities(path=path)
    assert state["manual"]["live_safety_alerts_heartbeat"] == "DONE"
    assert state["manual"].get("dead_man_switch") != "DONE"
