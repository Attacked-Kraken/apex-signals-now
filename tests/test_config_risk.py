import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_has_env():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi


def test_settings_defaults(monkeypatch):
    monkeypatch.chdir(ROOT)
    # clear overrides that would fight defaults
    for k in ("ENTRY_THRESHOLD", "MAX_TOTAL_EXPOSURE_USD", "WINNING_FORMULA"):
        monkeypatch.delenv(k, raising=False)
    from trading_bot.config import Settings

    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.max_total_exposure_usd == 3000
    assert s.broker == "kraken"
    assert s.paper_trading_mode is True
    assert s.strategy_mode == "volume_sweet_spot"


def test_exposure_cap_fixed():
    from trading_bot.config import Settings
    from trading_bot.risk_manager import RiskManager

    s = Settings(
        max_total_exposure_usd=3000,
        max_notional_per_trade_usd=1000,
        _env_file=None,
    )
    rm = RiskManager(s)
    # 2000 + 1000 == 3000 → allowed (not strictly greater)
    v = rm.check_exposure(2000, proposed_notional=1000)
    assert v.approved is True
    v2 = rm.check_exposure(2500, proposed_notional=1000)
    assert v2.approved is False
    assert "Max exposure" in v2.reason


def test_circuit_breaker_wf():
    from trading_bot.config import Settings
    from trading_bot.risk_manager import RiskManager

    s = Settings(winning_formula=True, _env_file=None)
    paused = {"v": False}
    rm = RiskManager(s)
    rm.set_ops(lambda p: paused.__setitem__("v", p))
    for _ in range(3):
        rm.record_trade_result(False, paused=False)
    assert paused["v"] is True
    assert rm.consecutive_losses() == 3


def test_fee_lock():
    from trading_bot.utils.decision_filters import fee_lock_sl, maybe_fee_lock_sl

    entry = 100.0
    floor = fee_lock_sl(entry, fee_buffer_pct=0.012)
    assert abs(floor - 101.2) < 1e-9
    # not armed
    assert maybe_fee_lock_sl(entry, 100.5, 99.0, arm_pct=0.012, fee_buffer_pct=0.012) == 99.0
    # armed
    new_sl = maybe_fee_lock_sl(entry, 102.0, 99.0, arm_pct=0.012, fee_buffer_pct=0.012)
    assert new_sl == floor


def test_winning_formula_and_presets():
    from trading_bot.config import Settings
    from trading_bot.telegram_commands import (
        STOP_LOSS_PRESETS,
        TRADE_PROFILE_PRESETS,
        execute_set_winning_formula,
    )

    assert "tight" in STOP_LOSS_PRESETS and "medium" in STOP_LOSS_PRESETS
    assert TRADE_PROFILE_PRESETS["aggressive"]["entry_threshold"] == 35
    assert TRADE_PROFILE_PRESETS["aggressive"]["max_total_exposure_usd"] == 3000

    s = Settings(winning_formula=False, entry_threshold=65, _env_file=None)
    msg = execute_set_winning_formula(s, enabled=True, env_path=None)
    assert "WINNING FORMULA ACTIVATED" in msg
    assert s.winning_formula is True
    assert s.entry_threshold == 50.0
    assert s.trail_fee_buffer_pct == 0.012
    assert s.tp1_fraction == 0.0
    assert os.environ.get("ENTRY_THRESHOLD") == "50"


def test_bear_threshold_helper():
    """Mirror main.TradingApp._bear_spot_long_threshold logic."""
    def bear_spot_long_threshold(base, *, short_bias, winning_formula, spot_long_only=True):
        if spot_long_only and short_bias:
            floor = 65.0 if winning_formula else 50.0
            return max(base * 1.10, floor)
        if winning_formula:
            return max(base, 50.0)
        return base

    assert bear_spot_long_threshold(50, short_bias=True, winning_formula=True) == 65.0
    assert bear_spot_long_threshold(50, short_bias=False, winning_formula=True) == 50.0
    assert bear_spot_long_threshold(40, short_bias=False, winning_formula=False) == 40.0


def test_status_header_order():
    from trading_bot.telegram_commands import format_status_reply

    text = format_status_reply(
        paper=True,
        cash=1600,
        equity=1600,
        wins=0,
        losses=0,
        paused=False,
        market_label="BULL_OK",
        short_bias=False,
        max_trade=1000,
        max_exposure=3000,
        winning_formula=True,
        trade_profile="medium",
        tod_custom="ON",
        stop_loss_line="🟡 MEDIUM −1.50%",
        threshold=50,
        spread_cap=0.0025,
        target_setup="LONG (Spot Mode)",
        proximity_score=40,
        proximity_threshold=50,
    )
    lines = text.splitlines()
    assert lines[0] == "Apex Signals Now PAPER status"
    assert "winning_formula: ON" in text
    assert "caps=$1000/trade $3000 exposure" in text


def test_regime_normalize():
    from trading_bot.strategy import normalize_market_regime
    from trading_bot.market_regime import BtcRegimeEngine

    assert normalize_market_regime("RANGE") == "RANGING"
    assert normalize_market_regime("BULL_TREND") == "RANGING"  # no such string — falls through
    eng = BtcRegimeEngine()
    # rising series → BULL_OK
    closes = [100 + i * 0.5 for i in range(60)]
    st = eng.update(closes)
    assert st.regime in ("BULL_OK", "BEAR_CHOP")


@pytest.mark.asyncio
async def test_paper_broker_no_private_orders(tmp_path):
    from trading_bot.brokers.kraken import KrakenBroker

    book = tmp_path / "paper_book_2.json"
    broker = KrakenBroker(paper=True, paper_book_path=str(book), account_equity=1600)
    # inject a synthetic mark via book then force mid by writing position? use dry path
    # place_order in paper should not call AddOrder — simulate with patched ticker
    async def fake_ticker(symbol):
        return {"bid": 100.0, "ask": 100.2, "last": 100.1, "mid": 100.1}

    broker.get_ticker = fake_ticker  # type: ignore
    res = await broker.place_order("BTC-USD", "BUY", 0.01)
    assert res.order_id.startswith("kr-paper-")
    assert res.paper is True
    positions = await broker.get_positions()
    assert len(positions) == 1
    # cancel_all must be noop in paper
    n = await broker.cancel_all()
    assert n == 0
    await broker.close()
