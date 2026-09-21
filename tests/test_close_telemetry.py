
import inspect
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.telegram_commands import format_history_reply


def test_set_consecutive_losses_on_class():
    assert hasattr(KrakenBroker, "set_consecutive_losses")
    assert "self" in inspect.signature(KrakenBroker.set_consecutive_losses).parameters


def test_history_shows_reason_and_peak():
    text = format_history_reply(
        [
            {
                "when": "2026-09-21",
                "side": "SELL",
                "symbol": "LINK-USD",
                "qty": 1.0,
                "price": 13.25,
                "pnl": 9.11,
                "reason": "SL",
                "peak_upl_pct": 1.25,
            }
        ]
    )
    assert "SL" in text
    assert "peak=+1.25%" in text
