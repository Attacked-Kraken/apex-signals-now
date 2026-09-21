# Apex Signals Now — Instance #2 (`cruzbot_instance_2`)

Kraken **paper-first** asyncio trading bot. Customer product name: **Apex Signals Now**. Internal checkout: `cruzbot_instance_2`.

Architectural source of truth: [`MASTER_SYSTEM_ARCHIVE.md`](./MASTER_SYSTEM_ARCHIVE.md).

## Security

- Grok / AI **only** via official xAI API (`https://api.x.ai/v1`, `XAI_API_KEY`). No browser/consumer chat automation.
- Rate limits + exponential backoff on Kraken, Telegram, and xAI.
- AI is sentiment/signal analyzer only — orders go through `KrakenBroker` / paper book.
- `.env` is gitignored. Never commit secrets.

## Paper runbook (quick start)

```bash
cd /workspace/cruzbot_instance_2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with real placeholders replaced as needed (Telegram/Kraken optional for dry smoke)

# Unset conflicting shell exports (process env overrides .env!)
unset ENTRY_THRESHOLD TRADE_PROFILE WINNING_FORMULA STOP_LOSS_PROFILE

# Paper loop
python main.py

# Smoke single cycle
python main.py --once

# Dry mock (no fills)
python main.py --dry-run --once
```

### Telegram

- Long-poll uses flock `/tmp/cruzbot_tg_{bot_id}.lock` (`bot_id = token.split(":")[0]`).
- If a second process starts, it stays **idle** (avoids duplicate `/status` replies).
- Keep **one** Instance #2 `main.py`. After a crash, clear stale lock only if no owner PID:
  `rm -f /tmp/cruzbot_tg_*.lock`

### Live gating (do not enable casually)

Live requires **all** of:

1. `PAPER_TRADING_MODE=false` in `.env`
2. `python main.py --live`
3. Telegram `/mode live` + `/confirm_live`

Never enable live without explicit operator request + `/confirm_live`.

## Risk defaults (archive)

- `MAX_TOTAL_EXPOSURE_USD=3000` — **fixed**, never equity-scaled
- `MAX_NOTIONAL_PER_TRADE_USD=1000` (typical)
- Winning Formula: threshold floors 50% BULL / 65% BEAR, circuit **3** consec losses
- Strategy mode: `volume_sweet_spot`
- BTC regime: `BEAR_CHOP` / `BULL_OK`; macro: `TRENDING` / `RANGING` / `HIGH_VOLATILITY`

## Layout

See archive §4. Key paths: `data/paper_book_2.json`, `data/trading_bot_2.db`.

## Symbols

Default allowlist: BTC-USD, ETH-USD, SOL-USD, LINK-USD, **XCN-USD** (Onyxcoin).
