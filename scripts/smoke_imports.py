#!/usr/bin/env python3
"""Sanity import check."""
from trading_bot import config, risk_manager
from trading_bot.brokers import kraken

print("OK", config.Settings, risk_manager.RiskManager, kraken.KrakenBroker)
