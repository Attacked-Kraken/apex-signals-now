# MASTER_SYSTEM_ARCHIVE.md

> **Product (customer-facing):** Apex Signals Now  
> **Internal checkout:** `cruzbot_instance_2` (Kraken paper Instance #2)  
> **Purpose:** Self-contained cold-start archive so a new LLM/operator can reconstruct behavior from real code.  
> **CRITICAL:** Never put real API keys, Telegram tokens, passwords, or secrets in this file. Use placeholders only.

---

## 1. EXECUTIVE ARCHITECTURE OVERVIEW

### What this bot is
Apex Signals Now Instance #2 is a **Python asyncio** paper/live trading engine focused on **Kraken spot** (`BROKER=kraken`), with **Telegram long-poll command control**, strategy mode **`volume_sweet_spot`**, and hard dollar risk caps.

- **Paper (default):** `PAPER_TRADING_MODE=true`. `KrakenBroker` simulates fills locally (`kr-paper-…`); it does **not** call Kraken `AddOrder` / `CancelOrder` / `CancelAll` while paper is on.
- **Live:** Requires `PAPER_TRADING_MODE=false` **and** `python main.py --live`, plus Telegram `/mode live` + `/confirm_live` (existing gating in `main.py`).
- **Public market data / WS:** Kraken REST + WS ticker used for marks; optional Binance/Bybit lead-lag / CVD / liq feeds for gates.

### Stack (modules)
| Role | Real path |
|------|-----------|
| Entry / loop / hard brackets / Telegram wiring | `main.py` (~6k lines) |
| Settings (pydantic-settings from `.env`) | `trading_bot/config.py` |
| Kraken adapter | `trading_bot/brokers/kraken.py` |
| Bars / indicators | `trading_bot/data_feed.py` |
| Strategy | `trading_bot/strategy_volume_sweet_spot.py` + `agent_core.py` |
| BTC regime | `trading_bot/market_regime.py` |
| Macro ATR+ADX regime | `trading_bot/strategy.py` (`TRENDING`/`RANGING`/`HIGH_VOLATILITY`) + `optimizer.py` |
| Risk / sizing / exposure | `trading_bot/risk_manager.py` + `utils/decision_filters.py` |
| Telegram commands | `trading_bot/telegram_commands.py` |
| Smart memory / quick-scalp | `trading_bot/adaptive_scalp.py` |

### Strategy + regimes (as implemented — do not invent names)
1. **Strategy:** `STRATEGY_MODE=volume_sweet_spot` — 5m low-volume retest / volume sweet spot, maker `POST_ONLY`, fee-to-target, structural guardrails.
2. **BTC spot regime** (`market_regime.py`):
   - `BEAR_CHOP` if BTC 15m close < EMA(50)
   - else `BULL_OK`
   - `dump_30m` if BTC dropped > ~1.2% in ~30m → treated as **SHORT bias** with BEAR_CHOP
3. **Macro optimizer regime** (`strategy.normalize_market_regime`): `TRENDING` | `RANGING` | `HIGH_VOLATILITY`  
   - Alias `RANGE` → `RANGING`. There is **no** string `BULL_TREND` in code; bullish BTC state is **`BULL_OK`**.
4. **SHORT bias effects (spot long-only, `ALLOW_PAPER_SHORTS=false`):**
   - Max concurrent positions → **1** (`_effective_max_concurrent`)
   - Spot still allows LONG evaluation (quality gate), not hard freeze
   - Entry threshold floored / raised (see §3)
   - WF BEAR SL clamp (see §3)

### Hard risk (code + instance)
- **`MAX_TOTAL_EXPOSURE_USD=3000`** — **fixed dollar cap**, never equity-scaled (`risk_manager.py`).
- **`MAX_NOTIONAL_PER_TRADE_USD`** — per-trade cap from Settings (instance `.env` often `1000`; profiles also set `1000`; WF BEAR **risk reference** uses **`$500`** notional for ≤`$6` dollar clamp).
- Circuit breaker: **3** consecutive losses if `WINNING_FORMULA`, else **5** → `ops.set_pause(True)` + Telegram alert; clear with `/resume`.
- Also: UTC-day **−3%** SQLite PnL circuit in strategy (`check_daily_drawdown_circuit`).

### Key paths (Instance #2)
```
cruzbot_instance_2/
  main.py
  trading_bot/…
  .env                          # secrets live here — never archive values
  data/paper_book_2.json        # paper positions/cash
  data/trading_bot_2.db         # SQLite (instance-isolated)
  data/active_params_2.json     # optimizer knobs
  data/trade_memory.json        # smart-memory outcomes
  data/trades.db                # pair blacklist source
```

### Locks / single poller
- **Telegram getUpdates single-poller lock (REAL):**  
  `/tmp/cruzbot_tg_{bot_id}.lock` where `bot_id = token.split(":")[0]`  
  Implemented in `TelegramCommandListener.run()` via `fcntl.flock(LOCK_EX|LOCK_NB)`. If lock held → listener stays **idle** (duplicate `/status` replies avoided).
- **`/tmp/cruzbot_paper.lock`:** **Not present in current source.** Ops may use PID files such as `/tmp/cruzbot2_main.pid`; do not invent a paper flock that isn’t in code.

### GOTCHA — shell env overrides `.env`
`Settings` uses pydantic-settings with `env_file=.env`. **Process environment variables override `.env` file values.**  
Telegram handlers that persist knobs also write `os.environ[...]` (e.g. WF sets `ENTRY_THRESHOLD=50`).  
If a shell still has `ENTRY_THRESHOLD=35` (from `/aggressive`) when you expect WF 50/65, **the shell wins on next Settings load / until process env is corrected.**  
WF `execute_set_winning_formula` and bare `/winning_formula` (while ON) explicitly force `os.environ["ENTRY_THRESHOLD"]="50"` to beat leftovers.

### Product naming
- Telegram `/status` header: **`Apex Signals Now PAPER|LIVE status`**
- Internal dirs/logs may say `cruzbot_instance_2` / CruzBot — keep customer copy as Apex Signals Now.

---

## 2. COMPLETE TELEGRAM COMMAND REGISTRY

Source of truth: `KNOWN_COMMANDS` + `BOT_COMMAND_SPECS` in `trading_bot/telegram_commands.py`. Wired in `main.py` → `_wire_telegram_commands()`.

### Full command list (`BOT_COMMAND_SPECS`)
| Command | Description |
|---------|-------------|
| `/status` | PAPER/LIVE snapshot |
| `/pause` | Skip new buys |
| `/resume` | Re-enable new buys |
| `/pnl` | Day P&L report |
| `/kill` | Flatten + stop loop |
| `/mode` | Show/switch PAPER/LIVE |
| `/confirm_live` | Confirm LIVE switch |
| `/set_limit` | Update size caps |
| `/set_threshold` | Entry threshold 15–95 |
| `/set_threshold_custom` | Custom entry threshold 15–95 |
| `/set_spread` | Max bid-ask spread % |
| `/tod_custom` | TOD gate on/off (locks vs profiles) |
| `/stop_loss` | SL profile tight/medium/free |
| `/winning_formula` | Winning formula on/off/status |
| `/aggressive` `/medium` `/low` | Trade profiles |
| `/profile` | Set aggressiveness profile |
| `/test_trade` | Paper ~$100 BUY |
| `/reset_paper` | Wipe paper book |
| `/wipe_paper` / `/factory_reset` | Full paper scratch |
| `/set` | Set knobs / profile alias |
| `/ping` | Heartbeat latency |
| `/positions` | Open positions |
| `/balance` | Cash / equity |
| `/history` | Last 5 trades |
| `/grok` | Grok sentiment |
| `/regime` | Market regime |
| `/logs` | Tail paper log |
| `/universe` | Crypto universe mode |
| `/universe_all` | Kraken discovery |
| `/universe_stocks` | Toggle xStocks |
| `/symbols` | List active pairs |
| `/close` | Close symbol (fee preview / confirm) |
| `/clear_positions` | Paper close xStocks (or all) at BE |
| `/help` | Command list |

Listener: only messages from `TELEGRAM_CHAT_ID`; long-poll `getUpdates`; `setMyCommands` registers menu.

---

### Deep dive: `/status`
Built by `format_status_reply()` — **layout order (do not reorder):**

1. `Apex Signals Now {PAPER|LIVE} status`
2. blank
3. `Last Scan Latency: {ms} across {N} pairs` (if available)
4. `last_tick_age=…`
5. blank ×2
6. `cash=$…` / `equity=$…` / `WR …% · nW/nL` (paper)
7. `pause=PAUSED…` **only if paused**
8. blank
9. `Market: …` (+ `bias=SHORT` when BEAR/dump)
10. `caps=$X/trade $Y exposure`
11. blank
12. Profile block:
    - `winning_formula: ON 🚀` | `OFF`
    - `Profile: AGGRESSIVE|MEDIUM|LOW`
    - `tod_custom: ON|OFF` (+ 🔒 if custom lock)
    - `stop_loss: {emoji} {LABEL} −{pct}%` (+ ` (WF BEAR clamp)` if clamped)
    - `Threshold: {N}%` (pass **effective** thresh via `_bear_spot_long_threshold`; may append smart-memory note)
    - `Spread cap: …%`
13. blank
14. `Target Setup: LONG (Spot Mode)` | `WAIT` (SHORT remapped to WAIT on spot)
15. `Entry Proximity: [bar] score%`
16. `focus=SYM @ $px` optional `[BLOCKED: …]`
17. blank
18. `positions: (none)` or per-position blocks (Progress bar, live, entry, pnl, qty, mv)
19. blank
20. `Universe: …` + Symbols expand footer / inline ▼ button

`main._cmd_status` injects market state, effective SL%, WF flag, smart-memory note on Threshold line.

---

### Deep dive: `/winning_formula [on|off|status]`
**Persistence keys:** `WINNING_FORMULA`, plus when ON: `ENTRY_THRESHOLD=50`, `MAX_CONCURRENT_POSITIONS=3`, `MIN_TP_PCT≈0.0305`, `TRAIL_FEE_BUFFER_PCT=0.012`, `ELITE_FEE_LOCK_ARM_PCT=0.012`, `TP1_FRACTION=0`, and forces `/stop_loss medium`.

**Core implementation** (`execute_set_winning_formula`):

```python
# trading_bot/telegram_commands.py (excerpt — real logic)

_WF_SAVED_KEYS = (
    "entry_threshold", "max_concurrent_positions", "min_tp_pct",
    "atr_bracket_tp_min_pct", "trail_fee_buffer_pct", "elite_fee_lock_arm_pct",
    "tp1_fraction", "stop_loss_profile",
)

def execute_set_winning_formula(settings, *, enabled, env_path, environ=None, signal_engine=None):
    updates = {ENV_KEY_WINNING_FORMULA: "true" if enabled else "false"}
    if enabled:
        # save prior knobs on settings._winning_formula_saved
        object.__setattr__(settings, "entry_threshold", 50.0)
        apply_runtime_entry_threshold(settings, 50.0)
        os.environ["ENTRY_THRESHOLD"] = "50"  # beat shell leftovers
        object.__setattr__(settings, "max_concurrent_positions", 3)  # BEAR cap enforced live → 1
        object.__setattr__(settings, "min_tp_pct", 0.0305)
        object.__setattr__(settings, "trail_fee_buffer_pct", 0.012)   # trail arm ≥ +1.20%
        object.__setattr__(settings, "elite_fee_lock_arm_pct", 0.012)
        object.__setattr__(settings, "tp1_fraction", 0.0)             # full exits only
        execute_set_stop_loss(settings, "medium", …)                 # auto SL → MEDIUM
        # persist ENTRY_THRESHOLD, MAX_CONCURRENT_POSITIONS, MIN_TP_PCT,
        # TRAIL_FEE_BUFFER_PCT, ELITE_FEE_LOCK_ARM_PCT, TP1_FRACTION
    else:
        # restore from _winning_formula_saved if present
    object.__setattr__(settings, "winning_formula", bool(enabled))
```

**Runtime effects (main.py, not only env):**
- Auto SL → MEDIUM; BEAR clamp **max −1.25%** and **≤$6 on $500** ref notional
- Threshold floors: **50% BULL / 65% BEAR** (via `_bear_spot_long_threshold`)
- Trail arm **+1.20%**; full exits; maker time-exits; circuit **3** losses
- Max **1** position in BEAR/chop (`_effective_max_concurrent`)
- Rebrackets open positions via `_apply_profile_brackets`
- **Bare `/winning_formula` while ON** (= status path): **re-applies** full formula + rebracket (fights `ENTRY_THRESHOLD=35` leftovers)

**Activated reply** (`format_winning_formula_activated`):
```
🚀 WINNING FORMULA ACTIVATED
• SL auto → 🟡 MEDIUM −1.50% / TP +2.25%
• BEAR clamp: max SL −1.25% (≤$6 risk on $500)
• BULL: full MEDIUM profile allowed
• Threshold 65% BEAR / 50% BULL · max 1 pos in BEAR
• Trail arm +1.20% · full exits · maker time-exits
• Circuit: 3 consec losses or −3% daily DD
```

Boot: `_enforce_winning_formula_sl()` forces MEDIUM if WF already true in Settings.

---

### Deep dive: `/stop_loss tight|medium|free`
**Presets (REAL):**

```python
STOP_LOSS_PRESETS = {
    "tight":  {"sl_pct": 0.0075, "sl_min_pct": 0.0075, "sl_max_pct": 0.0075,
               "tp_pct": 0.0115, "atr_mult": 1.0, "emoji": "🔴",
               "note": "Capital Preservation Active", "label": "TIGHT"},
    "medium": {"sl_pct": 0.015,  "sl_min_pct": 0.015,  "sl_max_pct": 0.015,
               "tp_pct": 0.0225, "atr_mult": 1.5, "emoji": "🟡",
               "note": "Standard Room", "label": "MEDIUM"},
    "free":   {"sl_pct": 0.025,  "sl_min_pct": 0.025,  "sl_max_pct": 0.030,
               "tp_pct": 0.0375, "atr_mult": 2.5, "emoji": "🟢",
               "note": "Wide Swing Room", "label": "FREE"},
}
```

**`execute_set_stop_loss` persists:** `STOP_LOSS_PROFILE`, `SL_MIN_PCT`, `SL_MAX_PCT`, `ELITE_ATR_SL_MULT`, `MIN_TP_PCT`, `ATR_BRACKET_TP_MIN_PCT`; hot-applies on Settings + signal_engine.

**Retroactive rebracket:** `main._cmd_stop_loss` walks open positions and calls `_apply_profile_brackets(sym, entry, qty=…, short=…)` so SL/TP update live.

Aliases: `loose|wide|free` → free; `med|balanced` → medium; `t|red` → tight.

---

### Deep dive: `/resume` (and `/pause`)
```python
async def _cmd_pause(...):
    self.ops.set_pause(True)
    return "Paused: new buys skipped; exits/brackets still managed."

async def _cmd_resume(...):
    self.ops.set_pause(False)
    return "Resumed: new buys enabled."
```
Use `/resume` after circuit-breaker pause (3/5 consec losses). Exits keep running while paused.

---

### Summaries of other commands
| Cmd | Behavior / persistence |
|-----|------------------------|
| `/tod_custom on\|off` | Sets `TOD_GATE_ENABLED` / `DISABLE_TOD_GATE` / `TOD_CUSTOM_LOCK=true` so `/aggressive` won’t flip TOD |
| `/set_threshold` / `_custom` | `ENTRY_THRESHOLD` 15–95; custom sets lock so profiles don’t overwrite |
| `/aggressive` `/medium` `/low` | **Full override** via `TRADE_PROFILE_PRESETS`; clears WF + custom locks; sets thresh/spread/TOD/RVOL/caps |
| `/set_limit <trade> <book>` | `MAX_NOTIONAL_PER_TRADE_USD`, `MAX_TOTAL_EXPOSURE_USD` |
| `/pnl` | Day closed trades + notifier performance report |
| `/positions` `/balance` `/history` | Paper book / ledger views |
| `/close <sym>` | Fee preview then confirm close |
| `/universe [all\|allowlist\|off]` | `SYMBOL_MODE` DYNAMIC_ALL / ALLOWLIST / OFF |
| `/universe_stocks` | Toggle Kraken xStocks |
| `/test_trade` | Paper-only ~$100 BUY |
| `/reset_paper` `/wipe_paper` | Paper book reset (paper mode only) |
| `/mode` `/confirm_live` | Paper↔live gating |
| `/kill` | Cancel, flatten, stop loop |
| `/regime` `/grok` `/logs` `/ping` `/help` | Info / ops |

**Profile presets (REAL):**
```python
TRADE_PROFILE_PRESETS = {
  "aggressive": {entry_threshold:35, max_spread_pct:0.005, disable_tod_gate:True,
                 rvol_breakout_mult:1.0, max_concurrent_positions:3,
                 agent_poll_seconds:12, max_notional_per_trade_usd:1000,
                 max_total_exposure_usd:3000},
  "medium":     {entry_threshold:65, max_spread_pct:0.0025, disable_tod_gate:False,
                 rvol_breakout_mult:2.0, max_concurrent_positions:2,
                 agent_poll_seconds:30, caps 1000/3000},
  "low":        {entry_threshold:80, max_spread_pct:0.0015, disable_tod_gate:False,
                 rvol_breakout_mult:2.5, max_concurrent_positions:2,
                 agent_poll_seconds:60, caps 1000/3000},
}
```
Note: `/aggressive` **turns WF off** (`WINNING_FORMULA=false`).

---

## 3. STRATEGY, REGIME & GUARDRAIL SPECIFICATIONS

### Fee-aware trailing / fee_lock
Constants (`utils/decision_filters.py`):
- Default `FEE_BUFFER` / `TRAIL_FEE_BUFFER_PCT` ≈ **0.0085 (0.85%)**; computed band can rise toward **~0.90–1.20%** from maker/taker + 0.10% pad, clamped `[0.0085, 0.012]`.
- WF sets buffer/arm to **0.012 (1.20%)**.

```python
def fee_lock_sl(entry, *, short=False, fee_buffer_pct=0.0085):
    # long: entry * (1 + FEE_BUFFER); short: entry * (1 - FEE_BUFFER)

def maybe_fee_lock_sl(entry, mark, current_sl, *, arm_pct=None, fee_buffer_pct=None, …):
    # Arm only if UPL >= arm (and arm >= buffer). Raise long SL to fee floor.
```

In `main._check_hard_brackets`:
1. Elite fee-lock via `maybe_fee_lock_sl` when `wants_elite_risk`.
2. Trail arms after UPL ≥ FEE_BUFFER; distance = `max(ATR14, 0.4% * entry)`.
3. Floor: once UPL ≥ buffer, ensure `SL ≥ Entry * (1 + FEE_BUFFER)`.

### TIME_EXIT_MAKER_BE
When `max_hold_minutes` exceeded and UPL in **[−0.50%, +0.80%]** (and not negative-elite-blocked):
- Arm pending maker BE: `be_px = entry * (1 + maker_fee_rate)` (default maker 0.5%).
- Deadline **+180s (3 min)**.
- If `mark >= be_px` → exit reason `TIME_EXIT_MAKER_BE` with limit at BE.
- If timeout and `mark < SL` → `TIME_EXIT_MAKER_TIMEOUT_SL` (market/SL path).
- Else wait (log `TIME_EXIT_MAKER_WAIT`).

### BEAR / SHORT bias threshold floors
```python
def _bear_spot_long_threshold(self, base: float) -> float:
    if spot_long_only and short_bias:
        floor = 65.0 if winning_formula else 50.0
        return max(base * 1.10, floor)   # +10% score requirement
    if winning_formula:
        return max(base, 50.0)           # BULL WF floor 50%
    return base
```

### WF BEAR SL clamp
```python
_WF_BEAR_SL_MAX = 0.0125          # −1.25%
_WF_BEAR_MAX_RISK_USD = 6.0
_WF_BEAR_REF_NOTIONAL = 500.0

# if WF and short_bias:
#   sl = min(profile_sl, 0.0125, 6/ref_notional)
#   tp = |sl| * 1.5 + 0.008   # 1.5 R:R + 0.80% fee buffer
# elif WF:
#   tp = |sl| * 1.5 + 0.008
```

### Circuit breaker (consec losses)
```python
limit = 3 if winning_formula else 5
if consecutive_losses() >= limit and not paused:
    ops.set_pause(True)
    notify: "⚠️ CIRCUIT BREAKER: {limit} consecutive losses … Use /resume"
```
Plus strategy daily DD: day SQLite PnL ≤ −3% of day-start equity → block new entries.

### Exposure / concurrent
- `max_total = settings.max_total_exposure_usd` (**3000** fixed).  
  Skip if `open_exposure + proposed_trade_cap > max_total`.
- `_effective_max_concurrent()` → **1** if SHORT bias else `max_concurrent_positions` (WF sets 3, but BEAR forces 1).

### Smart memory (aggressive only)
`adaptive_scalp.SmartMemory` on `data/trade_memory.json`:
- Rolling last **5** outcomes; if winrate < **40%** → tighten threshold **35 → 55**.
- **3** consecutive wins while tightened → restore **55 → 35**.
- `/status` may show `Threshold: N% (smart-memory tightened (w/n wins))`.
- Only applied when `wants_quick_scalp` (aggressive / thresh≤35).

### Partial TP disabled under WF / small books
- WF sets `tp1_fraction=0`.
- Hard brackets allow TP1 only if `tp1_fraction > 0` **and** notional > `PARTIAL_TP_MAX_NOTIONAL_USD` (default 1000).  
  Otherwise full exit at TP2 / profile TP / +2% progress logic.

### BTC regime vs macro regime (clarify naming)
| Layer | States | File |
|-------|--------|------|
| BTC 15m EMA | `BEAR_CHOP`, `BULL_OK` (+ `dump_30m`) | `market_regime.py` |
| Optimizer ATR+ADX | `TRENDING`, `RANGING` (`RANGE` alias), `HIGH_VOLATILITY` | `strategy.py` / `optimizer.py` |
| HIGH_VOLATILITY gate | Block 5m retest BUYs; halve trade notional | strategy + settings `REGIME_GATE_ENABLED` |

---

## 4. FULL CODEBASE BLUEPRINT

### Repo tree (key files under `cruzbot_instance_2`)
```
cruzbot_instance_2/
├── main.py                          # engine loop, brackets, Telegram handlers
├── requirements.txt
├── pyproject.toml
├── .env / .env.example / .env.instance2.example
├── INSTANCE_2_KRAKEN.md / ARCHITECTURE.md / PAPER_RUNBOOK.md
├── data/
│   ├── paper_book_2.json
│   ├── trading_bot_2.db
│   ├── active_params_2.json
│   ├── trade_memory.json
│   └── trades.db
├── trading_bot/
│   ├── config.py
│   ├── telegram_commands.py
│   ├── risk_manager.py
│   ├── strategy_volume_sweet_spot.py
│   ├── agent_core.py
│   ├── market_regime.py
│   ├── strategy.py                  # TRENDING/RANGING/HIGH_VOLATILITY
│   ├── adaptive_scalp.py            # SmartMemory, micro-trail
│   ├── data_feed.py
│   ├── executor.py / notifier.py / models.py / state_store.py
│   ├── structural_guardrails.py / scanner_auto_buy.py
│   ├── brokers/kraken.py (+ coinbase, alpaca, mock, base)
│   └── utils/decision_filters.py, entry_proximity.py, indicators.py, retry.py
├── tests/ …                         # pytest suite
├── scripts/ …                       # paper_session_status, telegram setup, …
└── deploy/cruzbot2.service …
```

### Name map (user shorthand → real files)
| Requested name | Actual |
|----------------|--------|
| `config.py` | `trading_bot/config.py` |
| `kraken_client.py` | `trading_bot/brokers/kraken.py` (+ `data_feed.py`) |
| `strategy_engine.py` | `strategy_volume_sweet_spot.py` + `agent_core.py` + `market_regime.py` |
| `risk_manager.py` | `trading_bot/risk_manager.py` + `utils/decision_filters.py` |
| `telegram_bot.py` | `telegram_commands.py` + `main.py` wiring |

### Critical code blocks (keep in sync with repo)

**STOP_LOSS_PRESETS / execute_set_stop_loss / execute_set_winning_formula** — see §2 (full logic copied from `telegram_commands.py`).

**`_profile_sl_tp_pct` / `_apply_profile_brackets` / `_bear_spot_long_threshold`** — see §3 (`main.py` ~2996–3070).

**Exposure check** (`risk_manager.py`):
```python
max_total = float(self.settings.max_total_exposure_usd)  # fixed; never equity-scaled
remaining = max_total - max(0.0, open_exposure_usd)
proposed = float(self.settings.max_notional_per_trade_usd or 0)
if remaining <= 0 or (proposed > 0 and open_exposure_usd + proposed > max_total):
    return RiskVerdict(approved=False, reason="⛔ ENTRY SKIPPED: Max exposure cap …")
```

**Maker time-exit path** — see §3 (`main._check_hard_brackets` pending dict `_pending_maker_time_exits`, arm band −0.5%…+0.8%, 180s timeout).

**Telegram lock** — `TelegramCommandListener.run()` flock on `/tmp/cruzbot_tg_{bot_id}.lock`.

### Dependencies (`requirements.txt`)
```
aiohttp>=3.9.0
websockets>=12.0
pydantic>=2.5.0
pydantic-settings>=2.1.0
pandas>=2.1.0
numpy>=1.26.0
pandas-ta>=0.3.14b
python-dotenv>=1.0.0
pytest>=7.4.0
pytest-asyncio>=0.23.0
coinbase-advanced-py>=1.8.0
httpx>=0.27.0
```
Instance ships a local `.venv` (Python 3.13 observed). Prefer:
```bash
cd /workspace/cruzbot_instance_2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Scrubbed `.env` template (Instance #2)
```bash
# --- Mode ---
PAPER_TRADING_MODE=true
DRY_RUN=false
BROKER=kraken
ACCOUNT_EQUITY=1600
SYMBOLS=BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD,DOGE-USD,DOT-USD,ATOM-USD,LTC-USD,UNI-USD,NEAR-USD
SYMBOL_MODE=ALLOWLIST
UNIVERSE_STOCKS=false
ALLOW_PAPER_SHORTS=false

# --- Kraken (PLACEHOLDERS ONLY) ---
KRAKEN_API_KEY=YOUR_KRAKEN_API_KEY
KRAKEN_API_SECRET=YOUR_KRAKEN_API_SECRET
KRAKEN_BASE_URL=https://api.kraken.com
KRAKEN_WS_URL=wss://ws.kraken.com/v2

# Leave unused brokers empty
COINBASE_API_KEY=
COINBASE_API_SECRET=

# --- Isolated Instance #2 paths ---
SQLITE_PATH=data/trading_bot_2.db
PAPER_BOOK_PATH=data/paper_book_2.json
ACTIVE_PARAMS_PATH=data/active_params_2.json
SQLITE_BACKUP_DIR=data/backups_2
TRADES_DB_PATH=data/trades.db

# --- Risk / strategy ---
STRATEGY_MODE=volume_sweet_spot
MAX_NOTIONAL_PER_TRADE_USD=1000
MAX_TOTAL_EXPOSURE_USD=3000
MAX_CONCURRENT_POSITIONS=3
ENTRY_THRESHOLD=50
TRADE_PROFILE=medium
STOP_LOSS_PROFILE=medium
WINNING_FORMULA=true
TRAIL_FEE_BUFFER_PCT=0.012
ELITE_FEE_LOCK_ARM_PCT=0.012
TP1_FRACTION=0
POST_ONLY=true
ELITE_RISK_ENABLED=true
BTC_REGIME_ENABLED=true
TOD_GATE_ENABLED=true
DISABLE_TOD_GATE=false

# --- Telegram (PLACEHOLDERS) ---
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID
TELEGRAM_COMMANDS_ENABLED=true

# --- Optional LLM / Grok ---
# USE_LLM=false
# XAI_API_KEY=YOUR_XAI_API_KEY

LOG_LEVEL=INFO
```

### How modules connect (runtime)
```
main.TradingApp
  ├─ Settings (.env) ──► RiskManager, Executor, DataFeed, AgentCore
  ├─ KrakenBroker (paper book JSON)
  ├─ BtcRegimeEngine ──► SHORT bias / BEAR_CHOP
  ├─ SymbolUniverse ──► allowlist / discovery / xStocks
  ├─ loop: scan symbols → proximity / strategy → risk → execute
  ├─ _check_hard_brackets each tick (SL/TP/trail/fee-lock/time-exit)
  └─ TelegramCommandListener (single flock) → _cmd_* handlers
```

---

## OPERATING NOTES (brief)

1. **Duplicate `main.py` PIDs break Telegram** — two pollers fight `getUpdates`; second loses flock and goes idle (or stale replies). Keep **one** Instance #2 process.
2. **Clear `/tmp/cruzbot_tg_*.lock` on restart** if the listener logs idle while no healthy process holds the lock (stale flock after crash). Prefer killing the old PID first.
3. **Never leave `ENTRY_THRESHOLD=35` in process env when WF wants 50/65** — shell overrides `.env`. After WF on, confirm `/status` Threshold and `os.environ["ENTRY_THRESHOLD"]`.
4. Paper state lives in `data/paper_book_2.json` — backup before `/wipe_paper`.
5. Do not share SQLite/paper book with Instance #1 (Coinbase).

---

## COLD-START REBOOT PROMPT

Copy-paste the following to a new AI to recreate and run Instance #2 paper-first:

```
You are rebuilding Apex Signals Now (internal: cruzbot_instance_2) — Kraken paper trading bot.

READ FIRST: /workspace/cruzbot_instance_2/MASTER_SYSTEM_ARCHIVE.md (this archive).
Then read real source under /workspace/cruzbot_instance_2 — do not invent features.

CHECKLIST:
1) Confirm tree: main.py, trading_bot/*, data/, requirements.txt, .env.instance2.example.
2) Create scrubbed .env from archive §4 template:
   - BROKER=kraken, PAPER_TRADING_MODE=true
   - PAPER_BOOK_PATH=data/paper_book_2.json, SQLITE_PATH=data/trading_bot_2.db
   - Placeholders only: YOUR_KRAKEN_API_KEY, YOUR_KRAKEN_API_SECRET,
     YOUR_TELEGRAM_BOT_TOKEN, YOUR_TELEGRAM_CHAT_ID, YOUR_XAI_API_KEY
   - Risk: MAX_TOTAL_EXPOSURE_USD=3000, MAX_NOTIONAL_PER_TRADE_USD per ops (often 1000);
     WINNING_FORMULA + STOP_LOSS_PROFILE=medium + ENTRY_THRESHOLD=50 as desired
3) python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
4) Unset conflicting shell exports (esp. ENTRY_THRESHOLD=35) before start:
     unset ENTRY_THRESHOLD TRADE_PROFILE WINNING_FORMULA STOP_LOSS_PROFILE
5) Ensure only one main.py for this instance. If Telegram idle:
     rm -f /tmp/cruzbot_tg_*.lock  (only after confirming no live owner PID)
6) Run paper:  python main.py
   Smoke:      python main.py --once
   Dry mock:   python main.py --dry-run --once
7) Verify Telegram: /status shows "Apex Signals Now PAPER status", caps, WF/profile/stop_loss/threshold.
8) Re-apply ops if needed: /winning_formula on   then   /status
9) Never commit secrets. Never enable live without explicit operator request + /confirm_live.

Reconstruct any missing behavior from archive §2–§4 and the cited functions in
telegram_commands.py / main.py / risk_manager.py / decision_filters.py /
market_regime.py / adaptive_scalp.py / strategy_volume_sweet_spot.py.
```

---

*End of MASTER_SYSTEM_ARCHIVE.md — generated from live `cruzbot_instance_2` source. Prefer re-reading code if archive and tree diverge.*
