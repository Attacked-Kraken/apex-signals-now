# Telegram commands — cruzbot_instance_2

Source of truth: `BOT_COMMAND_SPECS` / `KNOWN_COMMANDS` in
`trading_bot/telegram_commands.py`, wired by `TradingApp._wire_telegram_commands()`
in `main.py`. Listener: authorized `TELEGRAM_CHAT_ID` only, long-poll `getUpdates`,
exclusive flock `/tmp/cruzbot_tg_{bot_id}.lock` (unchanged). Grok uses official xAI
API only (`trading_bot/grok_client.py`).

## Parity vs production Instance #2 / cruzbot-v1 `instances/kraken-instance-2`

Production reference downloaded as `/workspace/apex-rebuild/compare/tg_i2.py`
(~3465 lines). Local rebuild aims for **command surface + formatter parity** with
paper-safe stubs where live Kraken discovery / fill pull is not ported cleanly.

| Area | Parity |
|------|--------|
| Command registry (`BOT_COMMAND_SPECS`) | **Full** — same command surface incl. production spellings `weekly_digest_101`, `circuity_breaker_manually` |
| Formatters / parsers (CB, digest, reset/wipe, universe, C2) | **Ported** from `tg_i2.py` |
| Confirm gates (~60s) for `/reset_paper` `/wipe_paper` | **Ported** (`OpsState` + formatters) |
| `/universe all\|allowlist\|off` + `/universe_stocks` | **Ported** parsers/replies; DYNAMIC_ALL discovery refresh is **stubbed** (keeps allowlist symbols) |
| `/weekly_digest_101` | **Ported** expectancy builder (paper DB); LIVE fill pull optional / empty if no `trades_live.db` |
| `/circuity_breaker_manually` | **Ported** on/off/status + `CIRCUIT_BREAKER_ENABLED` persist; losses always counted |
| `/positions` `/history` `/logs` `/ping` `/balance` | **Ported** formatters; history prefers `paper_ledger.db` then paper book closes |
| Inline keyboards / status Symbols expand | **Not ported** (text-only replies) |
| Full wipe of all production trading tables | **Ported** best-effort (`wipe_paper_artifacts`) |
| Live broker order paths behind `/close` etc. | Paper-first; LIVE still gated by `.env` + `--live` + `/confirm_live` |

`cruzbot-v1/instances/kraken-instance-2` was not present on this box; parity notes above
are vs the production `tg_i2.py` dump for Instance #2.

## Command list

| Command | Description | Handler status |
|---------|-------------|----------------|
| `/status` | PAPER/LIVE snapshot | Fully ported (local `format_status_reply`) |
| `/pause` | Skip new buys | Fully ported |
| `/resume` | Re-enable new buys | Fully ported |
| `/pnl` | Day P&L report | Fully ported |
| `/kill` | Flatten + stop loop | Fully ported |
| `/mode` | Show/switch PAPER/LIVE | Fully ported (simplified vs prod TTL confirm text) |
| `/confirm_live` | Confirm LIVE switch | Fully ported |
| `/set_limit` | Update size caps | Fully ported |
| `/set_threshold` | Entry threshold 15–95 | Fully ported |
| `/set_threshold_custom` | Custom entry threshold 15–95 | Fully ported |
| `/set_spread` | Max bid-ask spread % | Fully ported |
| `/tod_custom` | TOD gate on/off (locks vs profiles) | Fully ported |
| `/stop_loss` | SL profile tight/medium/free | Fully ported |
| `/winning_formula` | Winning formula on/off/status | Fully ported |
| `/weekly_digest_101` | Weekly digest paper\|live | **Added** — real `build_weekly_expectancy_digest` from `tg_i2` |
| `/circuity_breaker_manually` | CB auto on/off (counts always) | **Added** — real parse/format/execute from `tg_i2` |
| `/aggressive` `/medium` `/low` | Trade profiles | Fully ported |
| `/profile` | Set aggressiveness profile | Fully ported |
| `/test_trade` | Paper ~$100 BUY | Fully ported (paper-only) |
| `/reset_paper` | Wipe paper book | **Upgraded** — ~60s confirm + optional cash |
| `/wipe_paper` | Full paper scratch | **Upgraded** — ~60s confirm + artifact wipe |
| `/factory_reset` | Alias of `/wipe_paper` | **Upgraded** (same gates) |
| `/set` | Profile alias | Fully ported (profile subset) |
| `/ping` | Heartbeat latency | Fully ported (`format_ping_reply`) |
| `/positions` | Open positions | **Upgraded** — production-style formatter |
| `/balance` | Cash / equity | **Upgraded** — `format_balance_reply` |
| `/history` | Last 5 trades | **Upgraded** — ledger + book fallback |
| `/grok` | Grok sentiment | Fully ported (official xAI only) |
| `/regime` | Market regime | Fully ported (BTC regime engine) |
| `/logs` | Tail paper log | **Upgraded** — `read_tail_log_lines` + `redact_secrets` |
| `/universe` | Crypto universe mode | **Upgraded** — `all\|allowlist\|off` |
| `/universe_all` | Kraken discovery | Wired; discovery refresh stubbed |
| `/universe_stocks` | Toggle xStocks | **Upgraded** — on/off/toggle |
| `/symbols` | List active pairs | **Upgraded** — `format_symbols_reply` |
| `/close` | Close symbol (fee preview / confirm) | Fully ported |
| `/clear_positions` | Paper close at BE | Fully ported (paper-only) |
| `/help` | Command list | Fully ported |
| `/ban_risk` `/api_risk` | API ban-risk hygiene score | Fully ported |
| `/formula` `/formula_score` | Formula health 0–100 + ranked tips | **Added** — suggest-only (no auto param edits) |
| `/phd` `/phd_mode` | PHD ops pack + WEAK formula soft entry gate | **Added** — Tier-1 lock; tips not auto-applied |
| `/quant` `/quant_metrics` | Session risk metrics (DD/Sharpe/Sortino) + PHD DD gate status | **Added** — visibility only; DD gate when phd ON |

## Operator notes (behaviors)

### `/winning_formula [on|off|status]`
- ON applies the single Tier-1 pack: threshold/position limits, medium SL, fee + HWM/trail knobs, circuit breaker, caps/profile, and majors-only BTC/ETH/SOL/LINK/XCN.
- Changing a trading knob while ON immediately persists `WINNING_FORMULA=false` and reports which knob left the pack; it does not roll back that change.
- ON restores `data/wf_last_snapshot.json`; fit, strictly higher formula health observed by `/status` is saved to `data/wf_best_snapshot.json` and the last-ON snapshot without applying mid-session.
- Majors-only remains an internal WF/PHD setting and is not independently operator-toggleable.

### `/circuity_breaker_manually [on|off|status]`
- Bare / `status` → current ON/OFF, consecutive losses, trip hint.
- `on` / `off` → persists `CIRCUIT_BREAKER_ENABLED` to `.env` + runtime settings / `ops.cb_enabled`.
- Losses **always counted**; auto-pause only when CB is ON (`RiskManager`).

### `/reset_paper` / `/wipe_paper` / `/factory_reset`
- Two-step confirm (~60s TTL). Optional cash amount (default `ACCOUNT_EQUITY` / 1600).
- Paper-only; LIVE refused.
- `/wipe_paper confirm` also clears paper history DBs + truncates paper log and resets paper expectancy sources used by `/weekly_digest_101`.

### `/universe [all|allowlist|off]` + `/universe_stocks [on|off|toggle]`
- Production parsers/labels. `all` sets `DYNAMIC_ALL` (discovery refresh stubbed in this rebuild).

### `/weekly_digest_101 [paper|live]`
- 7-day expectancy digest from local DBs; live uses `trades_live.db` / optional fills.

### `/formula` / `/formula_score`
- Score 0–100 (higher = healthier). Bands: STRONG ≥70 🟢, OK 45–69 🟡, WEAK <45 🔴.
- `/status` shows compact `formula: 🟢 78` under `api_risk`.
- Full report lists component deltas + ranked adjustments with pasteable Telegram actions.
- Persists rolling history to `data/formula_memory.json` (gitignored). **Never auto-applies** parameter changes.

### `/phd` / `/phd_mode` [on|off|status]
- ON: forces winning_formula + Profile MEDIUM + stop_loss MEDIUM + circuit_breaker ON + majors_only; soft-blocks **new entries** when formula band is WEAK (exits OK). Does **not** auto-apply `/formula` tip knobs.
- Also soft-blocks **new entries** when session peak-to-now DD ≥ `PHD_MAX_DD_PCT` (default 8%; exits OK). See `/quant`.

### `/quant` / `/quant_metrics`
- Session risk health (not alpha proof): DD, peak, equity, Sharpe, Sortino, expectancy, n trades.
- Shows PHD DD limit and whether the DD gate would fire (gate active only when phd ON).
- `/status` appends compact `quant: DD … · Sortino … · n=…` when `QUANT_METRICS_ON_STATUS=true` (default).

## Packaging

```bash
python scripts/package_codebase.py
# or
python scripts/package_codebase.py --out /tmp/cruzbot_i2_sanitized.zip
```

Excludes `.env`, venv, caches, DBs, logs, credential-looking files. Keeps `.env.example`.

### `/live_safety`
- Reports mode, open-position count, auth/connectivity/stale-mark state, last OK timestamps, entry gate, heartbeat path/PID, and Kraken dead-man status.
- Enforcement is **LIVE only**. With LIVE positions open, Telegram alerts (5-minute per-kind cooldown):
  - `🚨 LIVE AUTH FAILURE — open positions require attention`
  - `🚨 LIVE CONNECTIVITY LOST — open positions may be unmanaged`
  - `🚨 LIVE STALE MARKS — ...`
- Any active LIVE auth/connectivity/stale gate pauses **new entries only**; exits/brackets remain attempted.
- Token-free `data/bot_heartbeat.json` updates each loop with UTC timestamp, mode, PID, and open-position count.
- Kraken `CancelAllOrdersAfter` 60s heartbeat is scaffolding only and honestly reports `not armed — private live path incomplete`. It does not issue private calls because signed live AddOrder/Cancel remains `NotImplemented`.

Sample:

```text
🛡 LIVE safety
mode: PAPER (paper — track only, no pause/alerts)
positions: 2 open
entries: allowed (live-only pause)
auth: OK  (consec=0, last OK never)
connectivity: OK  (consec=0, last OK 1s ago)
stale marks: OK  (ticker age 1s, gate 45s, only while positions open)
last private OK: never
dead-man: not armed — private live path incomplete
heartbeat: /workspace/cruzbot_instance_2/data/bot_heartbeat.json (pid 12345)
alerts: live+open-positions only; 5m cooldown
```

### `/future_pack`
- Reads current close count, expectancy, and current formula score; labels each deferred item `WAIT`, `READY`, or `DONE`.
- Persistent runtime state is `data/future_pack.json` (gitignored); canonical notes/gates are in `FUTURE_PACK.md`.
- Formula score in the 70s is a deadline/readiness goal, **not permission** to auto-implement risky features. It never changes trading knobs.
- If score is ≥65 and any prerequisite remains `WAIT`, it shows `🚨 URGENT — Before health score is in the 70s` without pretending the pack is complete.
- `v` does **not** greenlight regime-scaled size. Live safety alerts/heartbeat are `DONE`; dead-man remains `WAIT` until signed private Kraken AddOrder/Cancel exists.

Sample:

```text
📦 Future Pack readiness
formula: 🟡 48  ·  closes=2  ·  expectancy=$-1.38/trade
(score in the 70s is a deadline/goal — not permission to auto-implement)

• WAIT  Regime-scaled size
    2 closes; no explicit greenlight; no clean paper week ("v" ignored)
• WAIT  Maker-first + signed AddOrder/Cancel + dead-man
    blocked: live signed AddOrder is NotImplemented; dead-man not armed
• DONE  LIVE safety alerts + heartbeat file
    shipped: LIVE alerts + data/bot_heartbeat.json
• WAIT  Kraken CancelAllOrdersAfter dead-man
    not armed — private live path incomplete

Blocked prerequisite: live signed AddOrder (NotImplemented).
Do not auto-edit trading knobs. "v" is not a greenlight.
```
