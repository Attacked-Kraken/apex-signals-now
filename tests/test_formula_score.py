"""Unit tests for formula health score + smart memory (suggest-only)."""
from __future__ import annotations

import json
from pathlib import Path

from trading_bot.telegram_commands import format_status_reply
from trading_bot.utils.formula_score import (
    HISTORY_MAX,
    band_for_score,
    default_formula_memory_path,
    evaluate_formula,
    format_formula_report,
    format_formula_status_line,
    record_snapshot,
)


def test_band_thresholds():
    assert band_for_score(100) == "STRONG"
    assert band_for_score(70) == "STRONG"
    assert band_for_score(69) == "OK"
    assert band_for_score(45) == "OK"
    assert band_for_score(44) == "WEAK"
    assert band_for_score(0) == "WEAK"


def test_baseline_few_trades_near_55():
    snap = evaluate_formula(wins=0, losses=0, entry_threshold=60.0, winning_formula=True)
    assert 0 <= snap["score"] <= 100
    assert snap["band"] in ("STRONG", "OK", "WEAK")
    assert 45 <= snap["score"] <= 70  # baseline neighborhood
    assert snap["auto_apply"] is False


def test_low_wr_decreases_score_and_suggests_threshold():
    weak = evaluate_formula(
        wins=2,
        losses=8,
        consecutive_losses=3,
        entry_threshold=50.0,
        winning_formula=False,
        paused=False,
        circuit_breaker_on=True,
    )
    strong = evaluate_formula(
        wins=8,
        losses=2,
        consecutive_losses=0,
        entry_threshold=60.0,
        winning_formula=True,
    )
    assert weak["score"] < strong["score"]
    assert weak["band"] in ("WEAK", "OK")
    actions = " ".join(a["action"] for a in weak["adjustments"])
    assert "/set_threshold" in actions or "/winning_formula" in actions or "/pause" in actions
    # never claims auto-apply
    assert weak["auto_apply"] is False
    for a in weak["adjustments"]:
        assert "action" in a and a["action"]


def test_status_line_format():
    snap = {"score": 78, "band": "STRONG"}
    line = format_formula_status_line(snap)
    assert line == "formula: 🟢 78"
    assert format_formula_status_line({"score": 50, "band": "OK"}) == "formula: 🟡 50"
    assert format_formula_status_line({"score": 20, "band": "WEAK"}) == "formula: 🔴 20"


def test_report_contains_ranked_adjustments():
    snap = evaluate_formula(
        wins=1,
        losses=6,
        consecutive_losses=2,
        entry_threshold=40.0,
        winning_formula=False,
        focus_block_reason="insufficient cash",
        cash=50.0,
        max_total_exposure_usd=3000.0,
    )
    report = format_formula_report(snap)
    assert "Formula health:" in report
    assert "Ranked adjustments" in report
    assert "suggest-only" in report.lower() or "manual apply" in report.lower()
    assert "action:" in report
    assert snap["auto_apply"] is False


def test_memory_write_tmp_path(tmp_path: Path):
    mem = tmp_path / "formula_memory.json"
    snap = evaluate_formula(wins=3, losses=1, entry_threshold=65.0, winning_formula=True)
    data = record_snapshot(mem, snap)
    assert mem.exists()
    raw = json.loads(mem.read_text(encoding="utf-8"))
    assert "history" in raw
    assert len(raw["history"]) == 1
    assert raw["history"][0]["score"] == snap["score"]
    assert "top_adjustments" in raw["history"][0]
    assert "last_suggestions" in raw
    # no secret-looking keys
    blob = mem.read_text(encoding="utf-8").lower()
    for bad in ("api_key", "bot_token", "secret", "password"):
        assert bad not in blob
    # rolling cap
    for i in range(HISTORY_MAX + 5):
        s = evaluate_formula(wins=i % 3, losses=1, entry_threshold=60.0)
        record_snapshot(mem, s)
    raw2 = json.loads(mem.read_text(encoding="utf-8"))
    assert len(raw2["history"]) <= HISTORY_MAX
    assert data is not None


def test_default_memory_path_under_data():
    p = default_formula_memory_path()
    assert p.name == "formula_memory.json"
    assert p.parent.name == "data"


def test_format_status_reply_includes_formula_under_api_risk():
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
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        winning_formula=True,
        trade_profile="medium",
        universe_stocks=False,
        symbol_mode="ALLOWLIST",
    )
    assert "api_risk: 🟢 LOW (12/100)" in text
    assert "formula: 🟢 78" in text
    # layout: api_risk, blank, formula, blank, circuit breaker
    api_i = text.index("api_risk:")
    form_i = text.index("formula:")
    cb_i = text.index("circuit breaker")
    assert api_i < form_i < cb_i
    between_api_form = text[api_i:form_i]
    assert "\n\n" in between_api_form
    between_form_cb = text[form_i:cb_i]
    assert "\n\n" in between_form_cb


def test_over_filtering_threshold_tip():
    snap = evaluate_formula(
        wins=0,
        losses=0,
        entry_threshold=90.0,
        winning_formula=True,
        max_spread_pct=0.0025,
    )
    actions = [a["action"] for a in snap["adjustments"]]
    assert any(a.startswith("/set_threshold") for a in actions)
