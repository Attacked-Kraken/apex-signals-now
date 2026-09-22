"""Live safety tracker, alert dedupe, stale gate, heartbeat file."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trading_bot.utils.live_safety import (
    ALERT_AUTH,
    ALERT_CONNECTIVITY,
    ALERT_STALE_PREFIX,
    DEADMAN_NOT_ARMED,
    DEADMAN_PAPER,
    KIND_AUTH,
    LiveSafetyTracker,
    classify_kraken_error,
    format_dead_man_status,
    format_live_safety_report,
    write_bot_heartbeat,
)


def test_classify_auth_401_403_and_kraken_eapi():
    assert classify_kraken_error(status_code=401) == "auth"
    assert classify_kraken_error(status_code=403) == "auth"
    assert classify_kraken_error(RuntimeError("EAPI:Invalid key")) == "auth"
    assert classify_kraken_error("EAPI:Invalid signature") == "auth"
    assert classify_kraken_error("EGeneral:Permission denied") == "auth"
    exc = SimpleNamespace(status_code=403, response=None)
    assert classify_kraken_error(exc) == "auth"


def test_classify_timeout_and_other():
    assert classify_kraken_error(TimeoutError("read timed out")) == "connectivity"
    assert classify_kraken_error(RuntimeError("ConnectTimeout: kraken")) == "connectivity"
    assert classify_kraken_error(RuntimeError("connection refused")) == "connectivity"
    assert classify_kraken_error(RuntimeError("EGeneral:Internal error")) == "other"
    assert classify_kraken_error(status_code=500) == "other"


def test_tracker_consecutive_auth_and_reset():
    t = LiveSafetyTracker(auth_fail_threshold=2)
    t.note_auth_failure("EAPI:Invalid key")
    assert not t.auth_failed()
    t.note_auth_failure("401")
    assert t.auth_failed()
    t.note_auth_ok(now=1_700_000_000)
    assert not t.auth_failed()
    assert t.consecutive_auth_failures == 0
    assert t.last_auth_ok_ts == 1_700_000_000


def test_tracker_connectivity_threshold():
    t = LiveSafetyTracker(connectivity_fail_threshold=3)
    t.note_timeout("timeout 1")
    t.note_timeout("timeout 2")
    assert not t.connectivity_lost()
    t.note_timeout("timeout 3")
    assert t.connectivity_lost()
    t.note_connectivity_ok()
    assert not t.connectivity_lost()


def test_note_exception_routes_kinds():
    t = LiveSafetyTracker(auth_fail_threshold=1, connectivity_fail_threshold=1)
    assert t.note_exception(RuntimeError("EAPI:Invalid key"), source="private") == "auth"
    assert t.auth_failed()
    t2 = LiveSafetyTracker(connectivity_fail_threshold=1)
    assert t2.note_exception(TimeoutError("timed out"), source="ticker") == "connectivity"
    assert t2.connectivity_lost()


def test_alert_dedupe_five_minutes():
    t = LiveSafetyTracker(auth_fail_threshold=1, alert_cooldown_seconds=300.0)
    t.note_auth_failure("401")
    now = 1_000_000.0
    first = t.consume_alerts(live=True, positions_open=True, now=now)
    assert ALERT_AUTH in first
    second = t.consume_alerts(live=True, positions_open=True, now=now + 60)
    assert second == []
    third = t.consume_alerts(live=True, positions_open=True, now=now + 301)
    assert ALERT_AUTH in third
    # paper / no positions never alert
    t2 = LiveSafetyTracker(auth_fail_threshold=1)
    t2.note_auth_failure("401")
    assert t2.consume_alerts(live=False, positions_open=True, now=now) == []
    assert t2.consume_alerts(live=True, positions_open=False, now=now) == []


def test_stale_gate_only_with_open_positions():
    t = LiveSafetyTracker(stale_seconds=45.0)
    now = 5_000.0
    t.note_ticker_ok("BTC-USD", now=now)
    assert not t.stale_marks(positions_open=False, now=now + 120)
    assert not t.stale_marks(positions_open=True, now=now + 10)
    assert t.stale_marks(positions_open=True, now=now + 45)
    # never-seen ticker + open positions → stale
    fresh = LiveSafetyTracker(stale_seconds=45.0)
    assert fresh.stale_marks(positions_open=True, now=now)
    assert not fresh.stale_marks(positions_open=False, now=now)


def test_entries_paused_live_only():
    t = LiveSafetyTracker(auth_fail_threshold=1)
    t.note_auth_failure("403")
    assert t.entries_paused(live=True, positions_open=False)
    assert not t.entries_paused(live=False, positions_open=False)
    reason = t.entry_block_reason(live=True, positions_open=False)
    assert reason and "AUTH" in reason

    s = LiveSafetyTracker(stale_seconds=10.0)
    s.note_ticker_ok("ETH-USD", now=0.0)
    assert s.entries_paused(live=True, positions_open=True, now=30.0)
    assert not s.entries_paused(live=False, positions_open=True, now=30.0)
    assert not s.entries_paused(live=True, positions_open=False, now=30.0)


def test_stale_alert_text_and_connectivity_alert():
    t = LiveSafetyTracker(
        connectivity_fail_threshold=1,
        stale_seconds=10.0,
        alert_cooldown_seconds=300.0,
    )
    t.note_timeout("private timeout")
    t.note_ticker_ok("BTC-USD", now=0.0)
    # connectivity_ok from ticker reset timeouts — re-trip timeout after
    t.note_timeout("private timeout")
    msgs = t.consume_alerts(live=True, positions_open=True, now=20.0)
    assert ALERT_CONNECTIVITY in msgs
    assert any(m.startswith(ALERT_STALE_PREFIX) for m in msgs)


def test_heartbeat_file_token_free(tmp_path):
    path = tmp_path / "bot_heartbeat.json"
    payload = write_bot_heartbeat(
        path,
        mode="PAPER",
        pid=4242,
        open_positions=2,
        extra={"telegram_bot_token": "123:SECRET", "api_secret": "nope", "cycle": 9},
        now=1_700_000_123.0,
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["pid"] == 4242
    assert data["open_positions"] == 2
    assert data["mode"] == "PAPER"
    assert "unix" in data and "ts" in data
    dumped = path.read_text()
    assert "SECRET" not in dumped
    assert "token" not in dumped.lower()
    assert "api_secret" not in dumped
    assert payload.get("cycle") == 9


def test_dead_man_status_never_fakes_armed():
    assert format_dead_man_status(paper=True, signing_available=True, private_path_complete=True) == DEADMAN_PAPER
    assert (
        format_dead_man_status(paper=False, signing_available=False, private_path_complete=False)
        == DEADMAN_NOT_ARMED
    )
    assert (
        format_dead_man_status(paper=False, signing_available=True, private_path_complete=False)
        == DEADMAN_NOT_ARMED
    )


@pytest.mark.asyncio
async def test_kraken_dead_man_interface_does_not_post(monkeypatch):
    from trading_bot.brokers.kraken import KrakenBroker

    broker = KrakenBroker(api_key="k", api_secret="s", paper=False, paper_book_path="/tmp/no-book.json")
    # skip book ensure side effects already ran; no private HTTP
    posted = []

    async def _boom(*_a, **_k):
        posted.append(True)
        raise AssertionError("must not issue private request")

    monkeypatch.setattr(broker, "_public_get", _boom)
    if hasattr(broker, "_private_post"):
        monkeypatch.setattr(broker, "_private_post", _boom)
    assert broker.dead_man_status() == DEADMAN_NOT_ARMED
    assert broker.private_signing_available() is False
    assert broker.private_live_path_complete() is False
    out = await broker.cancel_all_orders_after(60)
    assert out["ok"] is False
    assert out["armed"] is False
    assert DEADMAN_NOT_ARMED in out["status"]
    assert posted == []


def test_format_live_safety_report_includes_fields():
    t = LiveSafetyTracker()
    t.note_ticker_ok("BTC-USD", now=1_700_000_000)
    snap = t.snapshot(
        live=False,
        positions_open=2,
        dead_man=DEADMAN_NOT_ARMED,
        heartbeat_path="data/bot_heartbeat.json",
        pid=99,
        now=1_700_000_003,
    )
    text = format_live_safety_report(snap)
    assert "🛡 LIVE safety" in text
    assert "PAPER" in text
    assert "positions: 2 open" in text
    assert "dead-man: not armed — private live path incomplete" in text
    assert "heartbeat:" in text
    assert "pid 99" in text
