""" /status delivery: batch marks + cache, no serial ticker waits. """
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.data_feed import DataFeed


@pytest.mark.asyncio
async def test_get_balances_batches_tickers(tmp_path: Path):
    book = tmp_path / "book.json"
    broker = KrakenBroker(
        paper=True, paper_book_path=str(book), account_equity=1600.0, min_public_interval=0.01
    )
    data = broker._read_book()
    data["positions"] = {
        "BTC-USD": {"symbol": "BTC-USD", "qty": 0.01, "entry": 100.0, "side": "long"},
        "ETH-USD": {"symbol": "ETH-USD", "qty": 1.0, "entry": 10.0, "side": "long"},
        "SOL-USD": {"symbol": "SOL-USD", "qty": 2.0, "entry": 5.0, "side": "long"},
    }
    broker._write_book(data)
    calls = {"n": 0}

    async def fake_public(path, params=None):
        calls["n"] += 1
        await asyncio.sleep(0)  # yield
        return {
            "XXBTZUSD": {"b": ["110"], "a": ["111"], "c": ["110.5"], "o": "100"},
            "XETHZUSD": {"b": ["12"], "a": ["13"], "c": ["12.5"], "o": "10"},
            "SOLUSD": {"b": ["6"], "a": ["7"], "c": ["6.5"], "o": "5"},
        }

    broker._public_get = fake_public  # type: ignore
    bal = await broker.get_balances()
    assert calls["n"] == 1
    assert bal["equity"] > 1600
    # second call within status stale window hits cache only
    calls["n"] = 0
    bal2 = await broker.get_balances(prefer_cache_max_age=8.0)
    assert calls["n"] == 0
    assert abs(bal2["equity"] - bal["equity"]) < 1e-9
    await broker.close()


@pytest.mark.asyncio
async def test_peek_cached_ticker_respects_max_age(tmp_path: Path):
    book = tmp_path / "book.json"
    broker = KrakenBroker(paper=True, paper_book_path=str(book), account_equity=1000.0)
    broker.cache_ticker("BTC-USD", {"bid": 1, "ask": 1, "last": 1, "mid": 1})
    assert broker.peek_cached_ticker("BTC-USD", max_age=5.0) is not None
    # Force age by rewriting timestamp
    broker._ticker_cache["BTC-USD"] = (time.time() - 30.0, {"bid": 1, "ask": 1, "last": 1, "mid": 1})
    assert broker.peek_cached_ticker("BTC-USD", max_age=8.0) is None
    assert broker.peek_cached_ticker("BTC-USD", max_age=60.0) is not None
    await broker.close()


def test_broker_ticker_cache_ttl_extended_for_status(tmp_path: Path):
    book = tmp_path / "book.json"
    broker = KrakenBroker(paper=True, paper_book_path=str(book), account_equity=1000.0)
    assert broker._ticker_cache_ttl >= 4.0
    assert broker._status_mark_max_age >= 20.0


def test_status_ttl_constants_extended():
    import main as main_mod

    assert main_mod._STATUS_MARK_MAX_AGE >= 20.0
    assert main_mod._STATUS_SNAP_TTL >= 12.0
    assert main_mod._STATUS_DAILY_TTL >= 90.0
    assert main_mod._STATUS_DAILY_MARK_MAX_AGE >= 120.0
    assert main_mod._STATUS_FETCH_TIMEOUT <= 1.0


@pytest.mark.asyncio
async def test_daily_board_prefers_peek_over_network(tmp_path: Path, monkeypatch):
    """Cold daily board must use peek/OHLC first — no get_tickers when marks exist."""
    from trading_bot.config import Settings
    import main as main_mod

    monkeypatch.chdir(tmp_path)
    book = tmp_path / "book.json"
    settings = Settings(
        paper_trading_mode=True,
        paper_book_path=str(book),
        telegram_commands_enabled=False,
        _env_file=None,
    )
    app = main_mod.TradingApp(settings, dry_run=True)
    calls = {"n": 0}

    async def boom_tickers(syms):
        calls["n"] += 1
        raise AssertionError("get_tickers should not run when peek has marks")

    app.broker.get_tickers = boom_tickers  # type: ignore
    for sym, px, open_px in (
        ("BTC-USD", 110.0, 100.0),
        ("ETH-USD", 12.0, 10.0),
        ("SOL-USD", 6.0, 5.0),
    ):
        app.broker.cache_ticker(
            sym,
            {
                "bid": px,
                "ask": px,
                "last": px,
                "mid": px,
                "open": open_px,
                "daily_pct": (px - open_px) / open_px * 100.0,
            },
        )

    lines = await app._status_daily_board_lines(["BTC-USD", "ETH-USD", "SOL-USD"])
    assert calls["n"] == 0
    assert lines and lines[0] == "Daily:"
    assert any("BTC" in ln for ln in lines)
    # Second call within TTL is pure cache
    lines2 = await app._status_daily_board_lines(["BTC-USD", "ETH-USD", "SOL-USD"])
    assert calls["n"] == 0
    assert lines2[0] == "Daily:"
    await app.shutdown()


@pytest.mark.asyncio
async def test_status_snap_cache_avoids_repeat_work(tmp_path: Path, monkeypatch):
    from trading_bot.config import Settings
    import main as main_mod

    monkeypatch.chdir(tmp_path)
    book = tmp_path / "book.json"
    settings = Settings(
        paper_trading_mode=True,
        paper_book_path=str(book),
        telegram_commands_enabled=False,
        quant_metrics_on_status=False,
        _env_file=None,
    )
    app = main_mod.TradingApp(settings, dry_run=True)
    resolve_calls = {"n": 0}
    orig = app._status_resolve_marks

    async def counted(symbols):
        resolve_calls["n"] += 1
        return await orig(symbols)

    app._status_resolve_marks = counted  # type: ignore
    snap1 = await app._status_market_snapshot()
    snap2 = await app._status_market_snapshot()
    assert snap1 is snap2 or snap1["ts"] == snap2["ts"]
    assert resolve_calls["n"] == 1  # second hit served from snap cache
    await app.shutdown()


@pytest.mark.asyncio
async def test_ohlc_soft_stale_returns_without_awaiting_network():
    feed = DataFeed(min_interval=0.01, cache_ttl=0.05, soft_ttl_mult=10.0)
    bars = [{"t": 1.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}] * 40
    feed._cache["BTC-USD:5"] = (time.time() - 0.1, bars)  # past hard TTL, inside soft
    fetches = {"n": 0}

    async def fake_fetch(symbol, interval):
        fetches["n"] += 1
        await asyncio.sleep(0.2)
        return bars

    feed._fetch_ohlc = fake_fetch  # type: ignore
    t0 = time.perf_counter()
    out = await feed.get_ohlc("BTC-USD", interval=5)
    elapsed = time.perf_counter() - t0
    assert out == bars
    assert elapsed < 0.05  # must not await the 0.2s fetch
    # background refresh kicked
    await asyncio.sleep(0.05)
    assert fetches["n"] >= 1
    await feed.close()


@pytest.mark.asyncio
async def test_get_ohlc_many_reuses_cache():
    feed = DataFeed(min_interval=0.01, cache_ttl=30.0)
    bars = [{"t": 1.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}] * 40
    feed._cache["BTC-USD:5"] = (time.time(), bars)
    feed._cache["ETH-USD:5"] = (time.time(), bars)
    fetches = {"n": 0}

    async def fake_fetch(symbol, interval):
        fetches["n"] += 1
        return bars

    feed._fetch_ohlc = fake_fetch  # type: ignore
    out = await feed.get_ohlc_many(["BTC-USD", "ETH-USD", "SOL-USD"], interval=5)
    assert "BTC-USD" in out and "ETH-USD" in out
    assert fetches["n"] == 1  # only SOL hard-miss
    await feed.close()


def test_ohlc_cache_default_raised():
    from trading_bot.config import Settings

    s = Settings(_env_file=None)
    # process env may still override; assert field default via model_fields
    field = Settings.model_fields["ohlc_cache_seconds"]
    assert float(field.default) >= 20.0


@pytest.mark.asyncio
async def test_daily_board_ohlc_only_triggers_ticker_fetch(tmp_path: Path, monkeypatch):
    """5m OHLC close without day open must not satisfy Daily % — fetch Ticker."""
    from trading_bot.config import Settings
    import main as main_mod

    monkeypatch.chdir(tmp_path)
    book = tmp_path / "book.json"
    settings = Settings(
        paper_trading_mode=True,
        paper_book_path=str(book),
        telegram_commands_enabled=False,
        _env_file=None,
    )
    app = main_mod.TradingApp(settings, dry_run=True)
    calls = {"n": 0}

    # Seed OHLC-only marks (no open / daily_pct) — previously caused n/a or wrong %.
    for sym, px in (("BTC-USD", 110.0), ("ETH-USD", 12.0)):
        app.broker.cache_ticker(
            sym, {"bid": px, "ask": px, "last": px, "mid": px}
        )

    async def fake_tickers(syms):
        calls["n"] += 1
        out = {}
        for s in syms:
            if s == "BTC-USD":
                last, open_px = 110.0, 100.0
            else:
                last, open_px = 12.0, 10.0
            out[s] = {
                "bid": last,
                "ask": last,
                "last": last,
                "mid": last,
                "open": open_px,
                "daily_pct": (last - open_px) / open_px * 100.0,
            }
        return out

    app.broker.get_tickers = fake_tickers  # type: ignore
    lines = await app._status_daily_board_lines(["BTC-USD", "ETH-USD"])
    assert calls["n"] == 1
    text = "\n".join(lines)
    assert "Daily:" in text
    assert "+10.00%" in text
    await app.shutdown()
