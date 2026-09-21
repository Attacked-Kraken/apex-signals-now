"""Chief Quant metrics — DD, Sortino, status line, PHD DD gate, status wiring."""
from __future__ import annotations

from trading_bot.telegram_commands import format_status_reply
from trading_bot.utils.quant_metrics import (
    compute_quant_snapshot,
    dd_failure_gate,
    expectancy,
    extract_trade_pnl,
    format_quant_report,
    format_quant_status_line,
    max_drawdown_pct,
    returns_from_closed_trades,
    sharpe_like,
    sortino_like,
    update_equity_peak,
)


def test_max_drawdown_pct_math():
    assert max_drawdown_pct(1000.0, 1000.0) == 0.0
    assert abs(max_drawdown_pct(1000.0, 920.0) - 8.0) < 1e-9
    assert abs(max_drawdown_pct(100.0, 50.0) - 50.0) < 1e-9
    # Above peak → 0
    assert max_drawdown_pct(1000.0, 1100.0) == 0.0
    # Bad peak
    assert max_drawdown_pct(0.0, 500.0) == 0.0
    assert max_drawdown_pct(-10.0, 5.0) == 0.0


def test_extract_pnl_field_variants():
    assert extract_trade_pnl({"pnl": 1.5}) == 1.5
    assert extract_trade_pnl({"realized_pnl": -2.0}) == -2.0
    assert extract_trade_pnl({"pl": 3.0}) == 3.0
    assert extract_trade_pnl({"net_pnl": 4.25}) == 4.25
    assert extract_trade_pnl({"foo": 1}) is None
    assert extract_trade_pnl(None) is None
    assert extract_trade_pnl("bad") is None
    # Prefer pnl over others when present
    assert extract_trade_pnl({"pnl": 1.0, "net_pnl": 9.0}) == 1.0


def test_sharpe_none_under_five():
    assert sharpe_like([1, 2, 3, 4]) is None
    assert sharpe_like([]) is None
    s = sharpe_like([1.0, -0.5, 0.8, -0.2, 1.2, 0.3])
    assert s is not None
    assert isinstance(s, float)


def test_sortino_with_downside():
    # Mix of wins/losses — Sortino uses downside only
    rets = [2.0, -1.0, 1.5, -0.5, 0.8, -2.0, 1.0]
    s = sortino_like(rets)
    assert s is not None
    # Mean positive-ish with downside → finite
    assert isinstance(s, float)
    # All positive → high / positive Sortino
    all_pos = sortino_like([1.0, 2.0, 0.5, 1.5, 0.8])
    assert all_pos is not None and all_pos > 0
    assert sortino_like([1, 2, 3]) is None  # <5


def test_expectancy_helper():
    # avg_loss as magnitude
    e = expectancy(10.0, 5.0, 0.5)
    assert abs(e - 2.5) < 1e-9  # 0.5*10 + 0.5*(-5)
    # signed loss
    e2 = expectancy(10.0, -5.0, 0.6)
    assert abs(e2 - (0.6 * 10 + 0.4 * -5)) < 1e-9


def test_format_quant_status_line_omits_missing():
    line = format_quant_status_line(dd_pct=3.2, sortino=1.1, n_trades=12)
    assert line == "quant: DD 3.2% · Sortino 1.1 · n=12"
    line2 = format_quant_status_line(dd_pct=4.1, sharpe=0.5, sortino=0.8, n_trades=9)
    assert "DD 4.1%" in line2
    assert "Sortino 0.8" in line2
    assert "Sharpe 0.5" in line2
    assert "n=9" in line2
    # DD only (few trades — Sharpe/Sortino None)
    line3 = format_quant_status_line(dd_pct=1.5, sharpe=None, sortino=None, n_trades=2)
    assert "DD 1.5%" in line3
    assert "Sharpe" not in line3
    assert "Sortino" not in line3
    assert "n=2" in line3


def test_dd_failure_gate_threshold():
    assert dd_failure_gate(dd_pct=7.9, limit_pct=8.0) is None
    reason = dd_failure_gate(dd_pct=8.0, limit_pct=8.0)
    assert reason is not None
    assert "8.0%" in reason
    assert "8%" in reason
    assert dd_failure_gate(dd_pct=12.5, limit_pct=8.0) is not None


def test_update_equity_peak():
    assert update_equity_peak(None, 1000.0) == 1000.0
    assert update_equity_peak(0, 500.0) == 500.0
    assert update_equity_peak(1000.0, 900.0) == 1000.0
    assert update_equity_peak(1000.0, 1100.0) == 1100.0


def test_compute_snapshot_and_report():
    trades = [
        {"pnl": 5.0, "won": True},
        {"net_pnl": -2.0, "won": False},
        {"realized_pnl": 3.0},
        {"pl": -1.0},
        {"pnl": 4.0},
        {"pnl": -0.5},
    ]
    snap = compute_quant_snapshot(
        equity=920.0,
        equity_peak=1000.0,
        closed_trades=trades,
        phd_mode=True,
        phd_max_dd_pct=8.0,
        formula_band="OK",
    )
    assert abs(snap["dd_pct"] - 8.0) < 1e-9
    assert snap["dd_gate_would_fire"] is True
    assert snap["n_trades"] == 6
    assert snap["sharpe"] is not None
    assert snap["sortino"] is not None
    report = format_quant_report(snap)
    assert "session risk health" in report
    assert "DD 8.0%" in report
    assert "FIRE" in report or "≥ 8%" in report
    assert "phd ON" in report

    # phd OFF → gate idle even if DD high
    snap2 = compute_quant_snapshot(
        equity=920.0,
        equity_peak=1000.0,
        closed_trades=trades,
        phd_mode=False,
        phd_max_dd_pct=8.0,
    )
    assert snap2["dd_gate_would_fire"] is False
    assert "idle" in format_quant_report(snap2).lower() or "phd OFF" in format_quant_report(snap2)


def test_returns_from_malformed_trades():
    trades = [
        {"pnl": "bad"},
        {"symbol": "BTC"},
        None,
        {"pnl": 1.0},
    ]
    # None entries may be skipped by isinstance check in returns_from_closed_trades
    # Our helper iterates trades — need to not crash on None
    cleaned = [t for t in trades if isinstance(t, dict)]
    rets = returns_from_closed_trades(cleaned)
    assert rets == [1.0]


def test_format_status_includes_quant_line():
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
        quant_line="quant: DD 4.1% · Sortino 0.8 · Sharpe 0.5 · n=9",
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        winning_formula=True,
        trade_profile="medium",
        universe_stocks=False,
        symbol_mode="ALLOWLIST",
    )
    assert "phd: ON" in text
    assert "quant: DD 4.1%" in text
    assert "Sortino 0.8" in text
    phd_i = text.index("phd: ON")
    quant_i = text.index("quant: DD")
    cb_i = text.index("circuit breaker")
    assert phd_i < quant_i < cb_i

    # Without quant_line — no quant fragment
    off = format_status_reply(
        paper_cash=1500.0,
        paper_equity=3000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        symbols=["BTC-USD"],
        phd_mode=False,
        circuit_breaker_on=True,
        winning_formula=True,
        trade_profile="medium",
    )
    assert "quant:" not in off
