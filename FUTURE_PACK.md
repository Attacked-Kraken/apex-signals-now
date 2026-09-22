# Future Pack — deferred items and readiness gates

Persistent notepad for work that is **not** auto-implemented. Telegram `/future_pack`
reads live closed-count, expectancy, and formula score and labels each item
`WAIT` / `READY` / `DONE`. Runtime state: `data/future_pack.json` (gitignored).

**A formula score in the 70s is a deadline / readiness goal, not permission to
auto-implement risky features.** Do not auto-edit trading knobs. The token `v`
is **not** a greenlight for regime-scaled size (or anything else).

## Gates

| Item | Ready when | Notes |
|------|------------|--------|
| Regime-scaled size | User **explicit** greenlight **or** a clean paper week | Not triggered by `v`. Size stays fixed until then. |
| Wider trail / partial TP | ≥30 closes **and** expectancy > 0, **plus** evidence HWM exits leave upside | Current HWM/trail stays as-is until READY **and** operator ships it. |
| Maker-first + signed AddOrder/Cancel + dead-man | Private HMAC signing + live AddOrder/Cancel + CancelAllOrdersAfter | **Required before live arm.** Live signed AddOrder is still `NotImplemented` — blocked prerequisite. |
| CVD / lead-lag | Baseline expectancy proven (≥30 closes, expectancy > 0) | Research overlay only after the baseline is real. |
| ATR brackets + hard daily DD gate | After ≥30 closes and positive expectancy | Daily DD can be added earlier if the user asks. |
| External watchdog / SMS routing | Before live real money | `data/bot_heartbeat.json` is token-free scaffolding; the supervisor/SMS path is not built. |
| LIVE safety alerts + heartbeat file | Shipped this revision | Auth / connectivity / stale-mark alerts (LIVE + open positions) + heartbeat file. |
| Kraken `CancelAllOrdersAfter` dead-man | Truly LIVE **and** private Kraken signing available | Interface exists; status stays `not armed — private live path incomplete` until signing + AddOrder exist. Do not fake success. |

## Urgent warning

If formula score is **≥ 65** (approaching the 70s) and any prerequisite is still
`WAIT`, `/future_pack` surfaces:

`🚨 URGENT — Before health score is in the 70s`

That is a **warning**, not a ship signal.

## What this revision does / does not do

- **Does:** LIVE safety tracker, Telegram `/live_safety` + `/future_pack`, heartbeat file, dead-man *interface*, this notepad.
- **Does not:** live signed `AddOrder` / `Cancel`, arm CancelAllOrdersAfter, change trail/TP/size, add CVD or ATR packs, or auto-apply formula tips.
