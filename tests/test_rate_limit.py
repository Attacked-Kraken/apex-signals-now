"""Anti-abuse API pacing: Retry-After parse, breaker, concurrency semaphore."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_bot.utils.http_errors import (
    RateLimitError,
    raise_for_rate_limit,
    retry_after_seconds,
)
from trading_bot.utils.rate_breaker import RateLimitBreaker
from trading_bot.utils.retry import with_exponential_backoff


def _resp(status: int, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return SimpleNamespace(status_code=status, headers=headers)


def test_retry_after_seconds_numeric():
    assert retry_after_seconds(_resp(429, "12")) == 12.0
    assert retry_after_seconds(_resp(429, "12.5")) == 12.5
    assert retry_after_seconds(_resp(429, None)) is None
    assert retry_after_seconds(_resp(429, "not-a-date")) is None


def test_retry_after_seconds_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    header = format_datetime(future)
    got = retry_after_seconds(_resp(503, header))
    assert got is not None
    assert 20.0 <= got <= 35.0


def test_raise_for_rate_limit_429_always():
    with pytest.raises(RateLimitError) as ei:
        raise_for_rate_limit(_resp(429), label="t")
    assert ei.value.status_code == 429
    assert ei.value.label == "t"
    assert ei.value.retry_after is None

    with pytest.raises(RateLimitError) as ei2:
        raise_for_rate_limit(_resp(429, "5"), label="x")
    assert ei2.value.retry_after == 5.0


def test_raise_for_rate_limit_503_needs_header():
    raise_for_rate_limit(_resp(503))  # no raise without Retry-After
    with pytest.raises(RateLimitError) as ei:
        raise_for_rate_limit(_resp(503, "8"))
    assert ei.value.status_code == 503
    assert ei.value.retry_after == 8.0


def test_raise_for_rate_limit_other_ok():
    raise_for_rate_limit(_resp(200))
    raise_for_rate_limit(_resp(404))


def test_breaker_trips_and_cools():
    b = RateLimitBreaker(trip_after=2, cooldown_seconds=2.0, max_cooldown=10.0)
    assert not b.cooling_down()
    b.record_rate_limit(retry_after=1.0)
    assert not b.cooling_down()
    b.record_rate_limit(retry_after=1.0)
    assert b.cooling_down()
    rem = b.remaining_seconds()
    # cooldown = min(10, max(2.0, 1.0)) = 2.0
    assert 1.0 <= rem <= 2.5
    assert "tripped" in b.trip_reason().lower() or "cooldown" in b.trip_reason().lower()
    b.record_success()
    # success resets consecutive but does not clear active cooldown
    assert b._consecutive == 0


def test_breaker_uses_retry_after_for_cooldown():
    b = RateLimitBreaker(trip_after=1, cooldown_seconds=5.0, max_cooldown=300.0)
    b.record_rate_limit(retry_after=90.0)
    assert b.cooling_down()
    # min(300, max(5, 90)) = 90
    assert 85.0 <= b.remaining_seconds() <= 95.0


def test_breaker_caps_at_max_cooldown():
    b = RateLimitBreaker(trip_after=1, cooldown_seconds=5.0, max_cooldown=30.0)
    b.record_rate_limit(retry_after=999.0)
    assert 25.0 <= b.remaining_seconds() <= 35.0


@pytest.mark.asyncio
async def test_backoff_honors_retry_after_and_reraises():
    calls = {"n": 0}
    cb = {"hit": 0}

    async def boom():
        calls["n"] += 1
        raise RateLimitError("rl", retry_after=0.05, status_code=429, label="t")

    def on_rl(exc: RateLimitError) -> None:
        cb["hit"] += 1

    t0 = time.perf_counter()
    with pytest.raises(RateLimitError):
        await with_exponential_backoff(
            boom,
            max_retries=1,
            base_delay=0.01,
            max_delay=1.0,
            label="test",
            on_rate_limit=on_rl,
        )
    elapsed = time.perf_counter() - t0
    assert calls["n"] == 2  # initial + 1 retry
    assert cb["hit"] == 2
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_ohlc_semaphore_limits_concurrency():
    """AgentCore should bound concurrent get_ohlc via settings.ohlc_fetch_concurrency."""
    from trading_bot.agent_core import AgentCore
    from trading_bot.config import Settings
    from trading_bot.models import Signal

    settings = Settings(ohlc_fetch_concurrency=2, _env_file=None)
    in_flight = {"n": 0, "peak": 0}
    lock = asyncio.Lock()

    class FakeFeed:
        breaker = None

        async def get_ohlc(self, symbol, interval=5):
            async with lock:
                in_flight["n"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
            await asyncio.sleep(0.05)
            async with lock:
                in_flight["n"] -= 1
            # minimal bars so score path short-circuits on "no bars"
            return []

    agent = AgentCore(settings, FakeFeed())  # type: ignore[arg-type]

    async def _noop_score(*a, **k):
        # bypass strategy — just exercise semaphore around get_ohlc
        return await AgentCore._score_one(agent, "BTC-USD", threshold=60, short_bias=False, allow_shorts=False)

    # Use scan which gathers _score_one
    signals, _ms = await agent.scan(
        ["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD"],
        threshold=60.0,
    )
    assert len(signals) == 4
    assert in_flight["peak"] <= 2
    assert in_flight["peak"] >= 1


def test_settings_pacing_defaults(monkeypatch):
    from pathlib import Path
    from trading_bot.config import Settings

    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    for k in (
        "KRAKEN_PUBLIC_MIN_INTERVAL",
        "OHLC_FETCH_CONCURRENCY",
        "RATE_LIMIT_TRIP_AFTER",
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "AGENT_POLL_JITTER_SECONDS",
    ):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.kraken_public_min_interval == 0.2
    assert s.ohlc_fetch_concurrency == 5
    assert s.rate_limit_trip_after == 2
    assert s.rate_limit_cooldown_seconds == 120.0
    assert s.agent_poll_jitter_seconds == 0.15
    assert s.agent_poll_live_seconds == 4.0
    assert s.live_max_orders_per_minute == 6
    assert s.api_risk_pause_on_high is True


def test_ops_state_has_cooldown_field():
    from trading_bot.state_store import OpsState

    ops = OpsState()
    assert ops.rate_limit_cooldown_until == 0.0
    ops.rate_limit_cooldown_until = time.time() + 10
    assert ops.rate_limit_cooldown_until > time.time()
