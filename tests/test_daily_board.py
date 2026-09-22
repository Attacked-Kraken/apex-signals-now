"""Daily % board formatting + rotation for /status."""
from __future__ import annotations

import pytest

from trading_bot.telegram_commands import (
    format_daily_board_line,
    format_daily_pct_token,
    format_daily_price_board,
    format_status_reply,
    rotate_daily_board,
    short_symbol_label,
)


def test_short_symbol_and_pct_token_plain():
    assert short_symbol_label("BTC-USD") == "BTC"
    assert short_symbol_label("ETH/USD") == "ETH"
    assert format_daily_pct_token(1.234) == "+1.23%"
    assert format_daily_pct_token(-0.856) == "-0.86%"
    assert format_daily_pct_token(0.0) == "+0.00%"
    assert format_daily_pct_token(None) == "n/a"
    from trading_bot.telegram_commands import format_daily_price
    assert format_daily_price(95123.45) == "$95,123.45"
    assert format_daily_price(3412.10) == "$3,412.10"
    assert format_daily_price(0.01342) == "$0.01342"
    # No colored circles
    s = format_daily_board_line("BTC-USD", 1.2, 95123.45)
    assert "🟢" not in s and "🔴" not in s
    assert "+1.20%" in s
    assert "$95,123.45" in s
    assert "BTC" in s


def test_rotate_top_to_bottom_full_and_window():
    rows = [{"symbol": f"S{i}-USD", "pct": float(i)} for i in range(5)]
    shown0, next0, more0 = rotate_daily_board(rows, 0)
    assert more0 == 0
    assert [r["symbol"] for r in shown0] == [f"S{i}-USD" for i in range(5)]
    assert next0 == 1
    shown1, next1, more1 = rotate_daily_board(rows, next0)
    assert [r["symbol"] for r in shown1][0] == "S1-USD"
    assert shown1[-1]["symbol"] == "S0-USD"
    assert next1 == 2
    assert more1 == 0

    big = [{"symbol": f"S{i}-USD", "pct": 0.0} for i in range(12)]
    shown, nxt, more = rotate_daily_board(big, 0, window=7)
    assert len(shown) == 7
    assert more == 5
    assert shown[0]["symbol"] == "S0-USD"
    shown2, _, more2 = rotate_daily_board(big, 1, window=7)
    assert shown2[0]["symbol"] == "S1-USD"
    assert more2 == 5


def test_format_daily_price_board_and_status_placement():
    rows = [
        {"symbol": "BTC-USD", "price": 95123.45, "pct": 1.23},
        {"symbol": "ETH-USD", "price": 3412.10, "pct": -0.85},
        {"symbol": "SOL-USD", "price": 145.6789, "pct": 0.0},
    ]
    lines, nxt = format_daily_price_board(rows, rot_index=0)
    assert lines[0] == "Daily:"
    assert lines[1] == ""
    assert "BTC" in lines[2] and "+1.23%" in lines[2]
    assert "ETH" in lines[3] and "-0.85%" in lines[3]
    assert nxt == 1
    lines2, nxt2 = format_daily_price_board(rows, rot_index=1)
    assert lines2[2].startswith("ETH")
    assert nxt2 == 2
    assert "🟢" not in "\n".join(lines) and "🔴" not in "\n".join(lines)

    big = [{"symbol": f"C{i}-USD", "pct": 0.1 * i} for i in range(10)]
    blines, _ = format_daily_price_board(big, rot_index=0, window=7)
    assert any(x.startswith("… +") for x in blines)

    text = format_status_reply(
        paper_cash=1000.0,
        paper_equity=1000.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        api_risk_line="api_risk: LOW (12/100)",
        formula_score_line="formula: 78",
        phd_mode=True,
        quant_line="quant: DD 1.0%",
        circuit_breaker_on=True,
        circuit_breaker_consec_losses=0,
        circuit_breaker_resume_seconds=90.0,
        daily_board_lines=lines,
    )
    # Classic vertical: header → scan/tick → api → formula → phd → quant → CB →
    # countdown → Daily → cash. No side-by-side _row packing.
    api_i = text.index("api_risk:")
    form_i = text.index("formula:")
    phd_i = text.index("phd:")
    quant_i = text.index("quant:")
    cb_i = text.index("circuit breaker")
    clock_i = text.index("⏰⏰")
    daily_i = text.index("Daily:")
    cash_i = text.index("cash=")
    assert api_i < form_i < phd_i < quant_i < cb_i < clock_i < daily_i < cash_i
    clock_line = [ln for ln in text.splitlines() if "⏰⏰" in ln][0]
    assert "Daily:" not in clock_line  # not mashed onto countdown
    assert "BTC" not in clock_line
    daily_section = text[daily_i:cash_i]
    assert daily_section.splitlines()[0] == "Daily:"
    assert daily_section.splitlines()[1] == ""
    assert "+1.23%" in daily_section and "-0.85%" in daily_section
    assert "$95,123.45" in daily_section and "$3,412.10" in daily_section
    assert "🟢" not in daily_section
    assert "🔴" not in daily_section


def test_compute_daily_pct_btc_eth_style():
    """Canonical daily %: (last - day_open) / day_open * 100."""
    from trading_bot.brokers.kraken import KrakenBroker

    # BTC-style: open 100_000 → last 101_250 = +1.25%
    assert KrakenBroker.compute_daily_pct(101_250.0, 100_000.0) == pytest.approx(1.25)
    # ETH-style: open 3_000 → last 2_955 = -1.50%
    assert KrakenBroker.compute_daily_pct(2_955.0, 3_000.0) == pytest.approx(-1.5)
    assert KrakenBroker.compute_daily_pct(100.0, 100.0) == pytest.approx(0.0)
    assert KrakenBroker.compute_daily_pct(100.0, 0.0) is None
    assert KrakenBroker.compute_daily_pct(None, 100.0) is None


def test_parse_ticker_row_string_open_not_char_index():
    """Kraken `o` is a string — must NOT use o[0] (that is char '8' → 8.0)."""
    from trading_bot.brokers.kraken import KrakenBroker

    row = {
        "b": ["85524.7", "1", "1"],
        "a": ["85524.8", "1", "1"],
        "c": ["85523.5", "0.01"],
        "o": "86598.00000",  # string form from live Kraken
    }
    out = KrakenBroker._parse_ticker_row(row)
    assert out["open"] == pytest.approx(86598.0)
    assert out["open"] != 8.0
    expected = (85523.5 - 86598.0) / 86598.0 * 100.0
    assert out["daily_pct"] == pytest.approx(expected)
    assert out["daily_pct"] == pytest.approx(
        KrakenBroker.compute_daily_pct(out["last"], out["open"])
    )


def test_parse_ticker_row_list_open():
    from trading_bot.brokers.kraken import KrakenBroker

    row = {
        "b": ["100", "1", "1"],
        "a": ["101", "1", "1"],
        "c": ["110", "1"],
        "o": ["100", "95"],  # [today, 24h]
    }
    out = KrakenBroker._parse_ticker_row(row)
    assert out["open"] == pytest.approx(100.0)
    assert out["open_24h"] == pytest.approx(95.0)
    assert out["daily_pct"] == pytest.approx(10.0)


def test_ticker_has_daily_rejects_ohlc_only():
    from trading_bot.brokers.kraken import KrakenBroker

    assert not KrakenBroker._ticker_has_daily(
        {"bid": 1, "ask": 1, "last": 1, "mid": 1}
    )
    assert not KrakenBroker._ticker_has_daily(
        {"bid": 1, "ask": 1, "last": 1, "mid": 1, "daily_pct": None, "open": 0}
    )
    assert KrakenBroker._ticker_has_daily(
        {"bid": 1, "ask": 1, "last": 110, "mid": 110, "open": 100, "daily_pct": 10.0}
    )


def test_cache_ticker_preserves_day_open_when_ohlc_seeds():
    from trading_bot.brokers.kraken import KrakenBroker
    from pathlib import Path
    import tempfile

    book = Path(tempfile.mkdtemp()) / "book.json"
    broker = KrakenBroker(paper=True, paper_book_path=str(book), account_equity=1000.0)
    broker.cache_ticker(
        "BTC-USD",
        {
            "bid": 110,
            "ask": 110,
            "last": 110,
            "mid": 110,
            "open": 100,
            "daily_pct": 10.0,
        },
    )
    broker.cache_ticker(
        "BTC-USD",
        {"bid": 111, "ask": 111, "last": 111, "mid": 111},  # OHLC-only seed
    )
    hit = broker.peek_cached_ticker("BTC-USD", max_age=60.0)
    assert hit is not None
    assert hit["open"] == pytest.approx(100.0)
    assert hit["last"] == pytest.approx(111.0)
    assert hit["daily_pct"] == pytest.approx(11.0)


def test_map_ticker_result_key_xbt_eth_xcn():
    from trading_bot.brokers.kraken import KrakenBroker

    pair_to_sym = {
        "XBTUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
        "XCNUSD": "XCN-USD",
        "SOLUSD": "SOL-USD",
    }
    assert KrakenBroker._map_ticker_result_key("XXBTZUSD", pair_to_sym) == "BTC-USD"
    assert KrakenBroker._map_ticker_result_key("XETHZUSD", pair_to_sym) == "ETH-USD"
    assert KrakenBroker._map_ticker_result_key("XCNUSD", pair_to_sym) == "XCN-USD"
    assert KrakenBroker._map_ticker_result_key("SOLUSD", pair_to_sym) == "SOL-USD"


def test_daily_rows_from_marks_recomputes_pct():
    import main as main_mod

    class _Dummy:
        pass

    app = _Dummy()
    app._daily_rows_from_marks = main_mod.TradingApp._daily_rows_from_marks.__get__(
        app, main_mod.TradingApp
    )
    rows = app._daily_rows_from_marks(
        ["BTC-USD", "ETH-USD"],
        {
            "BTC-USD": {"last": 101250.0, "open": 100000.0, "mid": 101250.0},
            "ETH-USD": {"last": 2955.0, "open": 3000.0, "mid": 2955.0},
        },
    )
    by = {r["symbol"]: r for r in rows}
    assert by["BTC-USD"]["pct"] == pytest.approx(1.25)
    assert by["ETH-USD"]["pct"] == pytest.approx(-1.5)
    assert by["BTC-USD"]["price"] == pytest.approx(101250.0)
