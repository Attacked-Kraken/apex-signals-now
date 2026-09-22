""" /status delivery: batch marks + cache, no serial ticker waits. """
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from trading_bot.brokers.kraken import KrakenBroker


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
            "XBTUSD": {"b": ["110"], "a": ["111"], "c": ["110.5"]},
            "ETHUSD": {"b": ["12"], "a": ["13"], "c": ["12.5"]},
            "SOLUSD": {"b": ["6"], "a": ["7"], "c": ["6.5"]},
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
