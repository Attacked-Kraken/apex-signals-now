"""Settings via pydantic-settings. Process env overrides .env file."""
from __future__ import annotations

from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Mode
    paper_trading_mode: bool = True
    dry_run: bool = False
    broker: str = "kraken"
    account_equity: float = 3000.0
    symbols: str = "BTC-USD,ETH-USD,SOL-USD,LINK-USD,XCN-USD"
    symbol_mode: str = "ALLOWLIST"
    universe_stocks: bool = False
    allow_paper_shorts: bool = False

    # Kraken
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    kraken_base_url: str = "https://api.kraken.com"
    kraken_ws_url: str = "wss://ws.kraken.com/v2"

    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""

    # Paths (Instance #2)
    sqlite_path: str = "data/trading_bot_2.db"
    paper_book_path: str = "data/paper_book_2.json"
    active_params_path: str = "data/active_params_2.json"
    sqlite_backup_dir: str = "data/backups_2"
    trades_db_path: str = "data/trades.db"
    trade_memory_path: str = "data/trade_memory.json"

    # Risk / strategy
    strategy_mode: str = "volume_sweet_spot"
    max_notional_per_trade_usd: float = 750.0
    max_total_exposure_usd: float = 3000.0  # fixed; never equity-scaled
    max_concurrent_positions: int = 3
    entry_threshold: float = 60.0
    trade_profile: str = "medium"
    stop_loss_profile: str = "medium"
    winning_formula: bool = True
    circuit_breaker_enabled: bool = True
    trail_fee_buffer_pct: float = 0.0125
    elite_fee_lock_arm_pct: float = 0.012
    tp1_fraction: float = 0.0
    post_only: bool = True
    elite_risk_enabled: bool = True
    btc_regime_enabled: bool = True
    tod_gate_enabled: bool = True
    disable_tod_gate: bool = False
    tod_custom_lock: bool = False
    caps_custom_lock: bool = False
    majors_only: bool = True
    entry_threshold_custom_lock: bool = False

    # SL/TP (hot-applied by profiles)
    sl_min_pct: float = 0.015
    sl_max_pct: float = 0.015
    min_tp_pct: float = 0.0225
    atr_bracket_tp_min_pct: float = 0.0225
    elite_atr_sl_mult: float = 1.5
    max_spread_pct: float = 0.0025
    rvol_breakout_mult: float = 2.0
    agent_poll_seconds: float = 5.0
    max_hold_minutes: float = 240.0
    taker_fee_rate: float = 0.008
    maker_fee_rate: float = 0.004
    partial_tp_max_notional_usd: float = 1000.0
    regime_gate_enabled: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_commands_enabled: bool = True

    # LLM / Grok (official xAI only)
    use_llm: bool = False
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-2-latest"
    xai_min_interval_seconds: float = 2.0

    log_level: str = "INFO"
    paused: bool = False

    def symbol_list(self) -> List[str]:
        return [s.strip() for s in self.symbols.split(",") if s.strip()]


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
