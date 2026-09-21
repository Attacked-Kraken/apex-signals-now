"""Telegram long-poll commands + flock single-poller + WF/profile helpers."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Tuple

import httpx

from trading_bot.utils.entry_proximity import progress_bar, proximity_bar
from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

Handler = Callable[[str, List[str]], Awaitable[str]]

BOT_COMMAND_SPECS: List[Tuple[str, str]] = [
    ("status", "PAPER/LIVE snapshot"),
    ("pause", "Skip new buys"),
    ("resume", "Re-enable new buys"),
    ("pnl", "Day P&L report"),
    ("kill", "Flatten + stop loop"),
    ("mode", "Show/switch PAPER/LIVE"),
    ("confirm_live", "Confirm LIVE switch"),
    ("set_limit", "Update size caps"),
    ("set_threshold", "Entry threshold 15–95"),
    ("set_threshold_custom", "Custom entry threshold 15–95"),
    ("set_spread", "Max bid-ask spread %"),
    ("tod_custom", "TOD gate on/off (locks vs profiles)"),
    ("stop_loss", "SL profile tight/medium/free"),
    ("winning_formula", "Winning formula on/off/status"),
    ("weekly_digest_101", "Weekly digest paper|live"),
    ("circuity_breaker_manually", "CB auto on/off (counts always)"),
    ("aggressive", "Trade profile aggressive"),
    ("medium", "Trade profile medium"),
    ("low", "Trade profile low"),
    ("profile", "Set aggressiveness profile"),
    ("test_trade", "Paper ~$100 BUY"),
    ("reset_paper", "Wipe paper book"),
    ("wipe_paper", "Full paper scratch"),
    ("factory_reset", "Full paper scratch"),
    ("set", "Set knobs / profile alias"),
    ("ping", "Heartbeat latency"),
    ("positions", "Open positions"),
    ("balance", "Cash / equity"),
    ("history", "Last 5 trades"),
    ("grok", "Grok sentiment"),
    ("regime", "Market regime"),
    ("logs", "Tail paper log"),
    ("universe", "Crypto universe mode"),
    ("universe_all", "Kraken discovery"),
    ("universe_stocks", "Toggle xStocks"),
    ("symbols", "List active pairs"),
    ("close", "Close symbol (fee preview / confirm)"),
    ("clear_positions", "Paper close xStocks (or all) at BE"),
    ("help", "Command list"),
]

KNOWN_COMMANDS = {c for c, _ in BOT_COMMAND_SPECS}

ENV_KEY_WINNING_FORMULA = "WINNING_FORMULA"

STOP_LOSS_PRESETS = {
    "tight": {
        "sl_pct": 0.0075,
        "sl_min_pct": 0.0075,
        "sl_max_pct": 0.0075,
        "tp_pct": 0.0115,
        "atr_mult": 1.0,
        "emoji": "🔴",
        "note": "Capital Preservation Active",
        "label": "TIGHT",
    },
    "medium": {
        "sl_pct": 0.015,
        "sl_min_pct": 0.015,
        "sl_max_pct": 0.015,
        "tp_pct": 0.0225,
        "atr_mult": 1.5,
        "emoji": "🟡",
        "note": "Standard Room",
        "label": "MEDIUM",
    },
    "free": {
        "sl_pct": 0.025,
        "sl_min_pct": 0.025,
        "sl_max_pct": 0.030,
        "tp_pct": 0.0375,
        "atr_mult": 2.5,
        "emoji": "🟢",
        "note": "Wide Swing Room",
        "label": "FREE",
    },
}

TRADE_PROFILE_PRESETS = {
    "aggressive": {
        "entry_threshold": 35.0,
        "max_spread_pct": 0.005,
        "disable_tod_gate": True,
        "rvol_breakout_mult": 1.0,
        "max_concurrent_positions": 3,
        "agent_poll_seconds": 12.0,
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
    "medium": {
        "entry_threshold": 65.0,
        "max_spread_pct": 0.0025,
        "disable_tod_gate": False,
        "rvol_breakout_mult": 2.0,
        "max_concurrent_positions": 2,
        "agent_poll_seconds": 30.0,
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
    "low": {
        "entry_threshold": 80.0,
        "max_spread_pct": 0.0015,
        "disable_tod_gate": False,
        "rvol_breakout_mult": 2.5,
        "max_concurrent_positions": 2,
        "agent_poll_seconds": 60.0,
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
}

_WF_SAVED_KEYS = (
    "entry_threshold",
    "max_concurrent_positions",
    "min_tp_pct",
    "atr_bracket_tp_min_pct",
    "trail_fee_buffer_pct",
    "elite_fee_lock_arm_pct",
    "tp1_fraction",
    "stop_loss_profile",
)


def _persist_env(env_path: Path, updates: Dict[str, str]) -> None:
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    keys_done = set()
    out: List[str] = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                keys_done.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in keys_done:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_runtime_entry_threshold(settings: Any, value: float) -> None:
    object.__setattr__(settings, "entry_threshold", float(value))
    os.environ["ENTRY_THRESHOLD"] = str(value)


def execute_set_stop_loss(
    settings: Any,
    profile: str,
    *,
    env_path: Optional[Path] = None,
    signal_engine: Any = None,
) -> str:
    key = profile.strip().lower()
    aliases = {
        "loose": "free",
        "wide": "free",
        "free": "free",
        "med": "medium",
        "balanced": "medium",
        "medium": "medium",
        "t": "tight",
        "red": "tight",
        "tight": "tight",
    }
    key = aliases.get(key, key)
    if key not in STOP_LOSS_PRESETS:
        return f"Unknown stop_loss profile: {profile}. Use tight|medium|free"
    preset = STOP_LOSS_PRESETS[key]
    object.__setattr__(settings, "stop_loss_profile", key)
    object.__setattr__(settings, "sl_min_pct", preset["sl_min_pct"])
    object.__setattr__(settings, "sl_max_pct", preset["sl_max_pct"])
    object.__setattr__(settings, "elite_atr_sl_mult", preset["atr_mult"])
    object.__setattr__(settings, "min_tp_pct", preset["tp_pct"])
    object.__setattr__(settings, "atr_bracket_tp_min_pct", preset["tp_pct"])
    os.environ["STOP_LOSS_PROFILE"] = key
    updates = {
        "STOP_LOSS_PROFILE": key,
        "SL_MIN_PCT": str(preset["sl_min_pct"]),
        "SL_MAX_PCT": str(preset["sl_max_pct"]),
        "ELITE_ATR_SL_MULT": str(preset["atr_mult"]),
        "MIN_TP_PCT": str(preset["tp_pct"]),
        "ATR_BRACKET_TP_MIN_PCT": str(preset["tp_pct"]),
    }
    if env_path:
        _persist_env(env_path, updates)
    if signal_engine is not None and hasattr(signal_engine, "on_stop_loss_change"):
        signal_engine.on_stop_loss_change(key, preset)
    return (
        f"{preset['emoji']} stop_loss → {preset['label']} "
        f"−{preset['sl_pct']*100:.2f}% / TP +{preset['tp_pct']*100:.2f}% "
        f"({preset['note']})"
    )


def format_winning_formula_activated() -> str:
    return (
        "🚀 WINNING FORMULA ACTIVATED\n"
        "• SL auto → 🟡 MEDIUM −1.50% / TP +2.25%\n"
        "• BEAR clamp: max SL −1.25% (≤$6 risk on $500)\n"
        "• BULL: full MEDIUM profile allowed\n"
        "• Threshold 65% BEAR / 50% BULL · max 1 pos in BEAR\n"
        "• Trail arm +1.20% · full exits · maker time-exits\n"
        "• Circuit: 3 consec losses or −3% daily DD"
    )


def execute_set_winning_formula(
    settings: Any,
    *,
    enabled: bool,
    env_path: Optional[Path] = None,
    environ: Optional[Dict[str, str]] = None,
    signal_engine: Any = None,
) -> str:
    env = environ if environ is not None else os.environ
    updates: Dict[str, str] = {ENV_KEY_WINNING_FORMULA: "true" if enabled else "false"}
    if enabled:
        saved = {k: getattr(settings, k, None) for k in _WF_SAVED_KEYS}
        object.__setattr__(settings, "_winning_formula_saved", saved)
        object.__setattr__(settings, "entry_threshold", 50.0)
        apply_runtime_entry_threshold(settings, 50.0)
        env["ENTRY_THRESHOLD"] = "50"  # beat shell leftovers
        object.__setattr__(settings, "max_concurrent_positions", 3)  # BEAR cap → 1 live
        object.__setattr__(settings, "min_tp_pct", 0.0305)
        object.__setattr__(settings, "atr_bracket_tp_min_pct", 0.0305)
        object.__setattr__(settings, "trail_fee_buffer_pct", 0.012)
        object.__setattr__(settings, "elite_fee_lock_arm_pct", 0.012)
        object.__setattr__(settings, "tp1_fraction", 0.0)
        execute_set_stop_loss(settings, "medium", env_path=env_path, signal_engine=signal_engine)
        updates.update(
            {
                "ENTRY_THRESHOLD": "50",
                "MAX_CONCURRENT_POSITIONS": "3",
                "MIN_TP_PCT": "0.0305",
                "TRAIL_FEE_BUFFER_PCT": "0.012",
                "ELITE_FEE_LOCK_ARM_PCT": "0.012",
                "TP1_FRACTION": "0",
            }
        )
        object.__setattr__(settings, "winning_formula", True)
        if env_path:
            _persist_env(env_path, updates)
        return format_winning_formula_activated()

    # restore
    saved = getattr(settings, "_winning_formula_saved", None) or {}
    for k, v in saved.items():
        if v is not None:
            object.__setattr__(settings, k, v)
            if k == "entry_threshold":
                apply_runtime_entry_threshold(settings, float(v))
    object.__setattr__(settings, "winning_formula", False)
    if env_path:
        _persist_env(env_path, updates)
    return "Winning Formula OFF — prior knobs restored where available."


def execute_set_trade_profile(
    settings: Any,
    name: str,
    *,
    env_path: Optional[Path] = None,
) -> str:
    key = name.strip().lower()
    if key not in TRADE_PROFILE_PRESETS:
        return f"Unknown profile: {name}. Use aggressive|medium|low"
    preset = TRADE_PROFILE_PRESETS[key]
    # clears WF + custom locks
    object.__setattr__(settings, "winning_formula", False)
    object.__setattr__(settings, "tod_custom_lock", False)
    object.__setattr__(settings, "entry_threshold_custom_lock", False)
    object.__setattr__(settings, "trade_profile", key)
    for attr, val in preset.items():
        object.__setattr__(settings, attr, val)
    apply_runtime_entry_threshold(settings, float(preset["entry_threshold"]))
    os.environ["WINNING_FORMULA"] = "false"
    os.environ["TRADE_PROFILE"] = key
    updates = {
        "WINNING_FORMULA": "false",
        "TRADE_PROFILE": key,
        "ENTRY_THRESHOLD": str(preset["entry_threshold"]),
        "MAX_SPREAD_PCT": str(preset["max_spread_pct"]),
        "DISABLE_TOD_GATE": "true" if preset["disable_tod_gate"] else "false",
        "RVOL_BREAKOUT_MULT": str(preset["rvol_breakout_mult"]),
        "MAX_CONCURRENT_POSITIONS": str(preset["max_concurrent_positions"]),
        "AGENT_POLL_SECONDS": str(preset["agent_poll_seconds"]),
        "MAX_NOTIONAL_PER_TRADE_USD": str(preset["max_notional_per_trade_usd"]),
        "MAX_TOTAL_EXPOSURE_USD": str(preset["max_total_exposure_usd"]),
    }
    if env_path:
        _persist_env(env_path, updates)
    return f"Profile → {key.upper()} (WF cleared). Threshold {preset['entry_threshold']:.0f}%"


def format_status_reply(
    *,
    paper: bool,
    cash: float,
    equity: float,
    wins: int,
    losses: int,
    paused: bool,
    market_label: str,
    short_bias: bool,
    max_trade: float,
    max_exposure: float,
    winning_formula: bool,
    trade_profile: str,
    tod_custom: str,
    stop_loss_line: str,
    threshold: float,
    threshold_note: str = "",
    spread_cap: float,
    target_setup: str,
    proximity_score: float,
    proximity_threshold: float,
    focus: str = "",
    positions_block: str = "positions: (none)",
    universe: str = "ALLOWLIST",
    last_scan_ms: Optional[float] = None,
    last_scan_n: Optional[int] = None,
    last_tick_age: Optional[str] = None,
) -> str:
    """Layout order exact to archive §2 deep-dive — do not reorder."""
    mode = "PAPER" if paper else "LIVE"
    lines: List[str] = [f"Apex Signals Now {mode} status", ""]
    if last_scan_ms is not None and last_scan_n is not None:
        lines.append(f"Last Scan Latency: {last_scan_ms:.0f}ms across {last_scan_n} pairs")
    if last_tick_age is not None:
        lines.append(f"last_tick_age={last_tick_age}")
    lines.append("")
    lines.append("")
    wr = (wins / (wins + losses) * 100.0) if (wins + losses) else 0.0
    lines.append(f"cash=${cash:,.2f}")
    lines.append(f"equity=${equity:,.2f}")
    lines.append(f"WR {wr:.0f}% · {wins}W/{losses}L")
    if paused:
        lines.append("pause=PAUSED (new buys skipped; exits still managed)")
    lines.append("")
    mkt = f"Market: {market_label}"
    if short_bias:
        mkt += "  bias=SHORT"
    lines.append(mkt)
    lines.append(f"caps=${max_trade:.0f}/trade ${max_exposure:.0f} exposure")
    lines.append("")
    lines.append(f"winning_formula: {'ON 🚀' if winning_formula else 'OFF'}")
    lines.append(f"Profile: {trade_profile.upper()}")
    lines.append(f"tod_custom: {tod_custom}")
    lines.append(f"stop_loss: {stop_loss_line}")
    thresh_line = f"Threshold: {threshold:.0f}%"
    if threshold_note:
        thresh_line += f" {threshold_note}"
    lines.append(thresh_line)
    lines.append(f"Spread cap: {spread_cap * 100:.2f}%")
    lines.append("")
    lines.append(f"Target Setup: {target_setup}")
    lines.append(f"Entry Proximity: {proximity_bar(proximity_score, proximity_threshold)}")
    if focus:
        lines.append(focus)
    lines.append("")
    lines.append(positions_block)
    lines.append("")
    lines.append(f"Universe: {universe}")
    return "\n".join(lines)


def format_position_block(
    symbol: str,
    entry: float,
    mark: float,
    qty: float,
    pnl_pct: float,
    progress_pct: float,
) -> str:
    mv = qty * mark
    return (
        f"{symbol}\n"
        f"  Progress {progress_bar(progress_pct)} {progress_pct:.0f}%\n"
        f"  live=${mark:,.4f}  entry=${entry:,.4f}\n"
        f"  pnl={pnl_pct*100:+.2f}%  qty={qty:.6f}  mv=${mv:,.2f}"
    )


# ---------------------------------------------------------------------------
# /circuity_breaker_manually — ported from production Instance #2 tg_i2.py
# ---------------------------------------------------------------------------

CIRCUITY_BREAKER_USAGE = "Usage: /circuity_breaker_manually [on|off|status]"


def parse_circuity_breaker_args(args: Sequence[str]) -> Optional[str]:
    """Return on|off|status|None (bare → status)."""
    if not args:
        return "status"
    a = str(args[0]).strip().lower()
    if a in ("on", "off", "status"):
        return a
    if a in ("1", "true", "enable", "enabled"):
        return "on"
    if a in ("0", "false", "disable", "disabled"):
        return "off"
    return None


def format_circuity_breaker_status(*, enabled: bool, consec: int = 0, tripped: bool = False) -> str:
    state = "ON" if enabled else "OFF"
    bits = [
        f"circuity_breaker_manually: {state}",
        f"{int(consec)} consecutive loss{'es' if int(consec) != 1 else ''}",
    ]
    if enabled:
        bits.append("auto pause + 45m cooldown when limit hit")
    else:
        bits.append("losses still counted — no auto pause; /resume if paused")
    if tripped:
        bits.append("TRIPPED (paused) — /resume to trade")
    return " · ".join(bits)


def execute_set_circuity_breaker(
    settings: Any,
    *,
    enabled: bool,
    env_path: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
    ops: Any = None,
    consec: int = 0,
    tripped: bool = False,
) -> str:
    """Persist CIRCUIT_BREAKER_ENABLED; losses always counted; auto-pause follows flag."""
    env = environ if environ is not None else os.environ
    flag = bool(enabled)
    try:
        object.__setattr__(settings, "circuit_breaker_enabled", flag)
    except Exception:  # noqa: BLE001
        pass
    env["CIRCUIT_BREAKER_ENABLED"] = "true" if flag else "false"
    if env_path:
        _persist_env(env_path, {"CIRCUIT_BREAKER_ENABLED": "true" if flag else "false"})
    if ops is not None:
        try:
            ops.cb_enabled = flag
            if not flag:
                # Turning CB off clears CB-active trip flag; does not unpause (user /resume).
                ops.cb_active = False
        except Exception:  # noqa: BLE001
            pass
    return format_circuity_breaker_status(enabled=flag, consec=int(consec or 0), tripped=bool(tripped) and flag)


# ---------------------------------------------------------------------------
# /weekly_digest_101 — ported from production Instance #2 tg_i2.py
# ---------------------------------------------------------------------------

WEEKLY_DIGEST_USAGE = "Usage: /weekly_digest_101 [paper|live]"


def parse_weekly_digest_args(args: Sequence[str]) -> Optional[str]:
    """Return 'paper'|'live'|None (None = use bot current mode)."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(WEEKLY_DIGEST_USAGE)
    a = str(args[0]).strip().lower()
    if a in ("paper", "p"):
        return "paper"
    if a in ("live", "l", "real"):
        return "live"
    if a in ("mode", "status", "?"):
        return None
    raise ValueError(WEEKLY_DIGEST_USAGE)


def _classify_exit_bucket(*, reason: str, entry: float, exit_px: float, pnl: float) -> str:
    """Map close → Hard SL / Fee Floor / Profit Runner / Other."""
    r = str(reason or "").upper()
    gross = 0.0
    try:
        if entry and float(entry) > 0 and exit_px is not None:
            gross = (float(exit_px) - float(entry)) / float(entry)
    except Exception:  # noqa: BLE001
        gross = 0.0
    if "PROFIT" in r and "RUNNER" in r:
        return "Profit Runner (>+2.50%)"
    if r in ("TP2", "TP", "TAKE_PROFIT", "TRAIL") or gross >= 0.025:
        if gross >= 0.025 or r in ("TP2", "TP", "TAKE_PROFIT"):
            return "Profit Runner (>+2.50%)"
    if (
        "FEE" in r
        or "MAKER_BE" in r
        or "TIME_EXIT_MAKER" in r
        or "RISK FREE" in r
        or (0.012 <= gross < 0.025)
    ):
        return "Fee Floor (+1.25% Lock)"
    if r in ("SL", "STOP_LOSS", "STOP", "TIME-STOP", "TIME_STOP") or gross < 0.012:
        if float(pnl) <= 0 or gross <= 0.0 or r in ("SL", "STOP_LOSS", "STOP", "TIME-STOP", "TIME_STOP"):
            return "Hard SL (-1.50%)"
    if float(pnl) > 0 and gross >= 0.012:
        return "Fee Floor (+1.25% Lock)"
    if float(pnl) > 0:
        return "Profit Runner (>+2.50%)"
    return "Hard SL (-1.50%)"


def build_weekly_expectancy_digest(
    *,
    days: int = 7,
    mode: str = "paper",
    trades_db: Optional[Path] = None,
    memory_db: Optional[Path] = None,
    root: Optional[Path] = None,
    live_closes: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """7-day expectancy digest. mode=paper|live. live_closes optional Kraken fills."""
    import sqlite3
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone
    from pathlib import Path as _Path

    mode_u = "LIVE" if str(mode).lower().startswith("live") else "PAPER"
    base = _Path(root) if root else _Path(__file__).resolve().parents[1]
    if mode_u == "LIVE":
        tdb = base / "data" / "trades_live.db"
    else:
        tdb = _Path(trades_db) if trades_db else base / "data" / "trades.db"
    mdb = _Path(memory_db) if memory_db else base / "data" / "trading_bot_2.db"
    days = max(1, min(int(days), 90))
    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=days)).timestamp()
    cutoff_iso = (now - timedelta(days=days)).isoformat()

    closes: List[Dict[str, Any]] = []
    if tdb.exists():
        try:
            conn = sqlite3.connect(str(tdb))
            rows = conn.execute(
                "SELECT symbol, entry, exit, net_pnl, timestamp FROM closed_trades "
                "WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff_ts,),
            ).fetchall()
            conn.close()
            for sym, entry, exit_px, pnl, ts in rows:
                closes.append(
                    {
                        "symbol": str(sym or "?"),
                        "entry": float(entry or 0),
                        "exit": float(exit_px or 0),
                        "pnl": float(pnl or 0),
                        "ts": float(ts or 0),
                        "reason": "",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("weekly digest closed_trades: %s", exc)

    # Match reasons from trade_memory (SELL)
    mem: List[Dict[str, Any]] = []
    if mdb.exists():
        try:
            conn = sqlite3.connect(str(mdb))
            rows = conn.execute(
                "SELECT ts, symbol, reason, pnl FROM trade_memory "
                "WHERE UPPER(action)='SELL' AND ts >= ? ORDER BY id ASC",
                (cutoff_iso,),
            ).fetchall()
            conn.close()
            for ts, sym, reason, pnl in rows:
                mem.append(
                    {
                        "ts": str(ts or ""),
                        "symbol": str(sym or "?"),
                        "reason": str(reason or ""),
                        "pnl": float(pnl or 0),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("weekly digest trade_memory: %s", exc)

    # Attach nearest memory reason by symbol + pnl proximity
    for c in closes:
        best = None
        best_score = 1e18
        for m in mem:
            if m["symbol"] != c["symbol"]:
                continue
            score = abs(float(m["pnl"]) - float(c["pnl"]))
            if score < best_score:
                best_score = score
                best = m
        if best is not None and best_score < 0.05:
            c["reason"] = best["reason"]

    if not closes and mem and mode_u == "PAPER":
        # fallback: memory-only window (paper)
        for m in mem:
            closes.append(
                {
                    "symbol": m["symbol"],
                    "entry": 0.0,
                    "exit": 0.0,
                    "pnl": m["pnl"],
                    "ts": 0.0,
                    "reason": m["reason"],
                }
            )

    if mode_u == "LIVE" and live_closes:
        closes = []
        for row in live_closes:
            closes.append(
                {
                    "symbol": str(row.get("symbol") or "?"),
                    "entry": float(row.get("entry") or 0),
                    "exit": float(row.get("exit") or row.get("price") or 0),
                    "pnl": float(row.get("pnl") or row.get("net_pnl") or 0),
                    "ts": float(row.get("ts") or 0),
                    "reason": str(row.get("reason") or "LIVE_FILL"),
                }
            )

    if not closes:
        return (
            f"📊 [WEEKLY DIGEST 101 - LAST {days} DAYS]\n"
            f"Mode: {mode_u}\n"
            "No closed trades in this window."
            + ("\n(Live: no Kraken fills in range / keys missing.)" if mode_u == "LIVE" else
               "\n(Paper: wipe or no closes yet — resets on /wipe_paper.)")
        )

    total = len(closes)
    net = sum(c["pnl"] for c in closes)
    wins = sum(1 for c in closes if c["pnl"] > 0)
    losses = sum(1 for c in closes if c["pnl"] <= 0)
    wr = 100.0 * wins / total if total else 0.0
    gross_wins = sum(c["pnl"] for c in closes if c["pnl"] > 0)
    gross_losses = abs(sum(c["pnl"] for c in closes if c["pnl"] < 0))
    pf = (gross_wins / gross_losses) if gross_losses > 1e-9 else (999.0 if gross_wins > 0 else 0.0)

    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for c in closes:
        b = _classify_exit_bucket(
            reason=c.get("reason") or "",
            entry=c.get("entry") or 0.0,
            exit_px=c.get("exit") or 0.0,
            pnl=c.get("pnl") or 0.0,
        )
        buckets[b]["count"] += 1
        buckets[b]["pnl"] += float(c["pnl"])

    by_sym: Dict[str, float] = defaultdict(float)
    by_sym_n: Dict[str, int] = defaultdict(int)
    for c in closes:
        by_sym[c["symbol"]] += float(c["pnl"])
        by_sym_n[c["symbol"]] += 1
    worst = sorted(by_sym.items(), key=lambda kv: kv[1])[:3]
    best = sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True)[:3]

    def money(x: float) -> str:
        return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"

    order = [
        "Hard SL (-1.50%)",
        "Fee Floor (+1.25% Lock)",
        "Profit Runner (>+2.50%)",
    ]
    lines = [
        f"📊 [WEEKLY DIGEST 101 - LAST {days} DAYS]",
        f"Mode: {mode_u}",
        "",
        f"• Total Trades: {total}",
        f"• Net P&L: {money(net)} (Fees Included)",
        f"• Win Rate: {wr:.1f}% ({wins}W / {losses}L)",
        f"• Profit Factor: {pf:.2f}" if pf < 900 else f"• Profit Factor: ∞",
        "",
        "📈 EXIT REASON BREAKDOWN:",
    ]
    shown = [k for k in order if k in buckets]
    shown.extend(sorted(k for k in buckets if k not in order))
    for i, k in enumerate(shown):
        branch = "└─" if i == len(shown) - 1 else "├─"
        lines.append(
            f" {branch} {k}: {int(buckets[k]['count'])} trades ({money(buckets[k]['pnl'])})"
        )

    lines.append("")
    lines.append("⚠️ BOTTOM 3 SYMBOLS (LEAKS):")
    if not worst or all(v >= 0 for _, v in worst):
        lines.append(" (none — no net losers this window)")
    else:
        for i, (sym, pnl) in enumerate(worst, 1):
            if pnl >= 0:
                continue
            lines.append(f" {i}. {sym}: {money(pnl)} ({by_sym_n[sym]} trades)")

    lines.append("")
    lines.append("🏆 TOP 3 SYMBOLS:")
    if not best or all(v <= 0 for _, v in best):
        lines.append(" (none — no net winners this window)")
    else:
        for i, (sym, pnl) in enumerate(best, 1):
            if pnl <= 0:
                continue
            lines.append(f" {i}. {sym}: {money(pnl)} ({by_sym_n[sym]} trades)")

    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Universe — ported from production Instance #2 tg_i2.py
# ---------------------------------------------------------------------------

def normalize_symbol_mode(raw: Any) -> str:
    """Return ALLOWLIST, DYNAMIC_ALL, or OFF (stocks-only)."""
    m = str(raw or "ALLOWLIST").strip().upper().replace("-", "_")
    if m in ("OFF", "NONE", "CRYPTO_OFF", "STOCKS_ONLY"):
        return "OFF"
    if m in ("DYNAMIC", "DYNAMIC_ALL", "ALL", "KRAKEN", "DISCOVERY"):
        return "DYNAMIC_ALL"
    return "ALLOWLIST"


def universe_friendly_label(mode: Any) -> str:
    """User-facing universe label (not internal enum jargon)."""
    normalized = normalize_symbol_mode(mode)
    if normalized == "DYNAMIC_ALL":
        return "Kraken discovery"
    if normalized == "OFF":
        return "Stocks only"
    return "Allow list"


def format_universe_line(
    mode: Any,
    count: int,
    *,
    stocks_enabled: Optional[bool] = None,
    stocks_count: Optional[int] = None,
) -> str:
    """Format the universe line, optionally including xStocks state."""
    base = f"Universe: {universe_friendly_label(mode)} ({int(count)})"
    if stocks_enabled is None:
        return base
    if stocks_enabled:
        return f"{base} | Stocks: ON ({int(stocks_count or 0)})"
    return f"{base} | Stocks: OFF"


UNIVERSE_USAGE = "Usage: /universe [all|allowlist|off]"


def parse_universe_args(args: Sequence[str]) -> Optional[str]:
    """None = show status; DYNAMIC_ALL / ALLOWLIST / OFF to switch. Raises ValueError."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(UNIVERSE_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("all", "dynamic", "dynamic_all", "discovery", "kraken"):
        return "DYNAMIC_ALL"
    if raw in ("allowlist", "allow", "hard", "default", "list"):
        return "ALLOWLIST"
    if raw in ("off", "none", "crypto_off", "stocks_only", "stocks-only"):
        return "OFF"
    raise ValueError(UNIVERSE_USAGE)


UNIVERSE_STOCKS_USAGE = "Usage: /universe_stocks [on|off|toggle]"


def parse_universe_stocks_args(args: Sequence[str]) -> Optional[bool]:
    """Return desired state, None for status/toggle, or raise ValueError.

    Caller treats None + empty args as status; None + toggle token as flip.
    Returns True/False for on/off. For toggle, raises is not used — return
    sentinel via dedicated parse: we return a special by convention:
    - no args → None (status)
    - on/off → bool
    - toggle → use ToggleSentinel via raising? Production returns None for toggle.
    """
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(UNIVERSE_STOCKS_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("on", "enable", "enabled", "true", "1"):
        return True
    if raw in ("off", "disable", "disabled", "false", "0"):
        return False
    if raw in ("toggle", "flip"):
        return None  # caller flips when args present and result is None
    raise ValueError(UNIVERSE_STOCKS_USAGE)


def format_universe_status(*, mode: str, count: int) -> str:
    label = universe_friendly_label(mode)
    return (
        f"Universe: {label} ({int(count)})\n"
        "Use /universe all, /universe allowlist, or /universe off to switch."
    )


def format_universe_switched(*, mode: str, count: int, refreshing: bool = False) -> str:
    label = universe_friendly_label(mode)
    extra = (
        " — refreshing liquid USD pairs…"
        if refreshing and normalize_symbol_mode(mode) == "DYNAMIC_ALL"
        else ""
    )
    return f"Universe → {label} ({int(count)}){extra}"


def format_symbols_reply(symbols: Sequence[str], *, mode: str = "ALLOWLIST") -> str:
    syms = [str(s) for s in symbols]

    def _is_xstock(sym: str) -> bool:
        base = sym.split("-", 1)[0]
        return base.endswith("x") or base.endswith("X")

    stocks = [s for s in syms if _is_xstock(s)]
    crypto = [s for s in syms if not _is_xstock(s)]
    label = universe_friendly_label(mode)
    parts = [f"Active pairs — {label} ({len(syms)} total)"]
    if stocks:
        parts.append(f"\nStocks / xStocks ({len(stocks)}):")
        for i in range(0, len(stocks), 5):
            parts.append("  " + ", ".join(stocks[i : i + 5]))
    else:
        parts.append("\nStocks / xStocks (0): (off or empty — /universe_stocks on)")
    if crypto:
        parts.append(f"\nCrypto ({len(crypto)}):")
        for i in range(0, len(crypto), 5):
            parts.append("  " + ", ".join(crypto[i : i + 5]))
    elif normalize_symbol_mode(mode) != "OFF":
        parts.append("\nCrypto (0): (empty allowlist)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# /reset_paper + /wipe_paper confirm gates (~60s) — from tg_i2.py
# ---------------------------------------------------------------------------

RESET_PAPER_CONFIRM_TTL_SECONDS = 60.0
RESET_PAPER_USAGE = (
    "Usage: /reset_paper [cash] then /reset_paper confirm "
    "(e.g. /reset_paper 2500 → /reset_paper confirm). "
    "Optional cash defaults to ACCOUNT_EQUITY / 1600."
)
REPLY_RESET_PAPER_LIVE_REFUSED = (
    "/reset_paper is PAPER ONLY — refused in LIVE mode "
    "(never touches live Kraken balances). Switch with /mode paper first."
)
REPLY_RESET_PAPER_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /reset_paper [cash] first "
    "(confirm within ~60s)."
)


class ResetPaperError(ValueError):
    """Invalid /reset_paper args or non-paper mode — callers must not wipe the book."""


WIPE_PAPER_CONFIRM_TTL_SECONDS = 60.0
WIPE_PAPER_USAGE = (
    "Usage: /wipe_paper [cash] then /wipe_paper confirm "
    "(e.g. /wipe_paper 1600 → /wipe_paper confirm). "
    "Alias: /factory_reset. "
    "Full paper scratch: book + history DB + paper logs (default ACCOUNT_EQUITY / 1600)."
)
REPLY_WIPE_PAPER_LIVE_REFUSED = (
    "/wipe_paper is PAPER ONLY — refused in LIVE mode "
    "(never touches live Kraken balances). Switch with /mode paper first."
)
REPLY_WIPE_PAPER_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /wipe_paper [cash] first "
    "(confirm within ~60s)."
)


class WipePaperError(ValueError):
    """Invalid /wipe_paper args or non-paper mode — callers must not wipe artifacts."""


def assert_paper_mode_for_wipe_paper(*, paper: bool) -> None:
    if not paper:
        raise WipePaperError(REPLY_WIPE_PAPER_LIVE_REFUSED)


def assert_paper_mode_for_reset_paper(*, paper: bool) -> None:
    if not paper:
        raise ResetPaperError(REPLY_RESET_PAPER_LIVE_REFUSED)


def _parse_reset_paper_cash(raw: str) -> float:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if s.endswith("%"):
        raise ResetPaperError(f"Invalid cash amount {raw!r}. {RESET_PAPER_USAGE}")
    try:
        val = float(s)
    except ValueError as exc:
        raise ResetPaperError(f"Invalid cash amount {raw!r}. {RESET_PAPER_USAGE}") from exc
    if not (val > 0) or val != val:
        raise ResetPaperError(f"Cash must be a positive number. {RESET_PAPER_USAGE}")
    if val > 10_000_000:
        raise ResetPaperError(f"Cash amount too large ({val:g}). {RESET_PAPER_USAGE}")
    return float(val)


def default_reset_paper_cash(account_equity: float | None = None) -> float:
    try:
        eq = float(account_equity) if account_equity is not None else 0.0
    except (TypeError, ValueError):
        eq = 0.0
    if eq > 0:
        return eq
    return 1600.0


def _fmt_reset_cash_arg(cash: float) -> str:
    s = f"{float(cash):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def parse_reset_paper_args(args: Sequence[str]) -> tuple[bool, float | None]:
    """Parse `/reset_paper` args → ``(confirm, cash_or_None)``."""
    if not args:
        return False, None
    tokens = [str(a).strip() for a in args if str(a).strip()]
    if not tokens:
        return False, None
    head = tokens[0].lower().replace("-", "_")
    if head == "confirm":
        if len(tokens) == 1:
            return True, None
        if len(tokens) == 2:
            return True, _parse_reset_paper_cash(tokens[1])
        raise ResetPaperError(RESET_PAPER_USAGE)
    if len(tokens) == 1:
        return False, _parse_reset_paper_cash(tokens[0])
    raise ResetPaperError(RESET_PAPER_USAGE)


def parse_wipe_paper_args(args: Sequence[str]) -> tuple[bool, float | None]:
    """Parse `/wipe_paper` args → ``(confirm, cash_or_None)``."""
    if not args:
        return False, None
    tokens = [str(a).strip() for a in args if str(a).strip()]
    if not tokens:
        return False, None
    head = tokens[0].lower().replace("-", "_")
    if head == "confirm":
        if len(tokens) == 1:
            return True, None
        if len(tokens) == 2:
            try:
                return True, _parse_reset_paper_cash(tokens[1])
            except ResetPaperError as exc:
                msg = str(exc).replace("/reset_paper", "/wipe_paper").replace(
                    RESET_PAPER_USAGE, WIPE_PAPER_USAGE
                )
                raise WipePaperError(msg) from exc
        raise WipePaperError(WIPE_PAPER_USAGE)
    if len(tokens) == 1:
        try:
            return False, _parse_reset_paper_cash(tokens[0])
        except ResetPaperError as exc:
            msg = str(exc).replace("/reset_paper", "/wipe_paper").replace(
                RESET_PAPER_USAGE, WIPE_PAPER_USAGE
            )
            raise WipePaperError(msg) from exc
    raise WipePaperError(WIPE_PAPER_USAGE)


def format_reset_paper_pending_reply(
    cash: float, *, ttl_seconds: float = RESET_PAPER_CONFIRM_TTL_SECONDS
) -> str:
    cash_s = f"{float(cash):.2f}"
    arg = _fmt_reset_cash_arg(cash)
    return (
        f"Confirm paper book RESET to cash=${cash_s} (wipes open paper positions). "
        f"Reply `/reset_paper confirm` or `/reset_paper confirm {arg}` "
        f"within ~{int(ttl_seconds)}s."
    )


def format_reset_paper_done_reply(
    cash: float,
    *,
    equity: float | None = None,
    positions: int = 0,
    account_equity_updated: bool = False,
) -> str:
    eq = float(equity if equity is not None else cash)
    msg = (
        f"Paper book reset: cash=${float(cash):.2f} equity=${eq:.2f} "
        f"positions={int(positions)}. Check /status."
    )
    if account_equity_updated:
        msg += (
            f" ACCOUNT_EQUITY={_fmt_reset_cash_arg(cash)} saved to .env "
            "(baseline sticks across restarts)."
        )
    return msg


def format_wipe_paper_pending_reply(
    cash: float, *, ttl_seconds: float = WIPE_PAPER_CONFIRM_TTL_SECONDS
) -> str:
    cash_s = f"{float(cash):.2f}"
    arg = _fmt_reset_cash_arg(cash)
    return (
        f"Confirm FULL paper WIPE to cash=${cash_s} "
        f"(book + history DB + paper logs; open positions wiped). "
        f"Reply `/wipe_paper confirm` or `/wipe_paper confirm {arg}` "
        f"within ~{int(ttl_seconds)}s. Alias: /factory_reset confirm."
    )


def format_wipe_paper_done_reply(
    cash: float,
    *,
    equity: float | None = None,
    positions: int = 0,
    account_equity_updated: bool = False,
    history_cleared: bool = True,
    logs_cleared: bool = True,
    ledger_rows_deleted: int = 0,
    trading_tables_cleared: Optional[Dict[str, int]] = None,
) -> str:
    eq = float(equity if equity is not None else cash)
    tables = trading_tables_cleared or {}
    tables_s = (
        ",".join(f"{k}:{v}" for k, v in sorted(tables.items())) if tables else "none"
    )
    msg = (
        f"Paper factory wipe: cash=${float(cash):.2f} equity=${eq:.2f} "
        f"positions={int(positions)}. "
        f"history_cleared={'yes' if history_cleared else 'no'} "
        f"(ledger_rows={int(ledger_rows_deleted)}; tables={tables_s}). "
        f"logs_cleared={'yes' if logs_cleared else 'no'}. "
        f"Check /status /history /logs."
    )
    if account_equity_updated:
        msg += (
            f" ACCOUNT_EQUITY={_fmt_reset_cash_arg(cash)} saved to .env "
            "(baseline sticks across restarts)."
        )
    return msg


WIPE_PAPER_TRADING_DB_TABLES = (
    "events",
    "trade_memory",
    "buy_dedupe",
    "symbol_lockouts",
    "post_stop_cooldown",
    "trade_failures",
)


def wipe_paper_artifacts(
    *,
    project_root: Any,
    sqlite_path: Optional[Any] = None,
    ledger_path: Optional[Any] = None,
    log_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Clear paper history DBs + truncate paper log. Never deletes .env or DB files."""
    import sqlite3
    from pathlib import Path as _Path

    root = _Path(project_root)
    ledger = _Path(ledger_path) if ledger_path else root / "data" / "paper_ledger.db"
    trading_db = _Path(sqlite_path) if sqlite_path else root / "data" / "trading_bot_2.db"
    if log_path is not None:
        log = _Path(log_path)
    else:
        resolved = resolve_instance_log_path(root)
        log = _Path(resolved) if resolved is not None else root / "data" / "paper_loop.log"

    summary: Dict[str, Any] = {
        "ledger_path": str(ledger),
        "ledger_rows_deleted": 0,
        "trading_db_path": str(trading_db),
        "trading_tables_cleared": {},
        "log_path": str(log),
        "log_truncated": False,
    }

    if ledger.exists():
        try:
            conn = sqlite3.connect(str(ledger))
            try:
                try:
                    n = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
                except Exception:  # noqa: BLE001
                    n = 0
                conn.execute("DELETE FROM events")
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
                except Exception:  # noqa: BLE001
                    pass
                conn.commit()
                summary["ledger_rows_deleted"] = n
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wipe_paper ledger clear failed (%s): %s", ledger, exc)

    if trading_db.exists():
        cleared: Dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(trading_db))
            try:
                existing = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table in WIPE_PAPER_TRADING_DB_TABLES:
                    if table not in existing:
                        continue
                    try:
                        n = int(
                            conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                        )
                    except Exception:  # noqa: BLE001
                        n = 0
                    conn.execute(f"DELETE FROM [{table}]")
                    try:
                        conn.execute(
                            "DELETE FROM sqlite_sequence WHERE name=?", (table,)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    cleared[table] = n
                conn.commit()
            finally:
                conn.close()
            summary["trading_tables_cleared"] = cleared
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wipe_paper trading DB clear failed (%s): %s", trading_db, exc
            )

    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("")
        summary["log_truncated"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("wipe_paper log truncate failed (%s): %s", log, exc)

    trades_db = root / "data" / "trades.db"
    summary["trades_db_cleared"] = 0
    if trades_db.exists():
        try:
            conn = sqlite3.connect(str(trades_db))
            try:
                try:
                    n = int(conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0])
                except Exception:  # noqa: BLE001
                    n = 0
                conn.execute("DELETE FROM closed_trades")
                try:
                    conn.execute("DELETE FROM blacklist")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='closed_trades'")
                except Exception:  # noqa: BLE001
                    pass
                conn.commit()
                summary["trades_db_cleared"] = n
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wipe_paper trades.db clear failed: %s", exc)

    mem_json = root / "data" / "trade_memory.json"
    try:
        if mem_json.exists():
            mem_json.write_text(
                '{"outcomes": [], "tightened": false, "session_wins": 0, "session_losses": 0}\n',
                encoding="utf-8",
            )
            summary["trade_memory_json_reset"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("wipe_paper trade_memory.json reset failed: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# C2 formatters: positions / balance / history / logs — from tg_i2.py
# ---------------------------------------------------------------------------

def _tg_fmt_price(val: float) -> str:
    v = float(val)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _tg_fmt_qty(val: float) -> str:
    v = float(val)
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


def _fmt_opt_price(val: Any) -> str:
    if val is None:
        return "n/a"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "n/a"
    if f != f:
        return "n/a"
    return f"${_tg_fmt_price(f)}"


def format_ping_reply(latency_ms: float) -> str:
    return f"pong {float(latency_ms):.0f}ms"


def format_positions_reply(
    positions: Sequence[Dict[str, Any]],
    *,
    paper: bool = True,
) -> str:
    mode = "PAPER" if paper else "LIVE"
    if not positions:
        return f"Open positions ({mode}): (none)"
    lines = [f"Open positions ({mode}) — {len(positions)}:"]
    for p in positions:
        sym = p.get("symbol") or "?"
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        entry = p.get("avg_entry_price")
        pnl_pct = p.get("pnl_pct")
        if pnl_pct is None:
            try:
                upl = p.get("unrealized_pl")
                if upl is not None and entry is not None and float(entry) > 0 and qty:
                    cost = float(entry) * abs(qty)
                    if cost > 0:
                        pnl_pct = 100.0 * float(upl) / cost
            except (TypeError, ValueError):
                pnl_pct = None
        if pnl_pct is None:
            pnl_s = "n/a"
        else:
            try:
                pct = float(pnl_pct)
                pnl_s = f"{pct:+.2f}%"
            except (TypeError, ValueError):
                pnl_s = "n/a"
        sl = _fmt_opt_price(p.get("stop_loss"))
        tp = _fmt_opt_price(p.get("take_profit"))
        entry_s = _fmt_opt_price(entry)
        lines.append(
            f"  {sym} qty={_tg_fmt_qty(qty)} entry={entry_s} pnl={pnl_s} sl={sl} tp={tp}"
        )
    return "\n".join(lines)


def format_balance_reply(
    *,
    paper: bool,
    cash: float,
    equity: float,
    available: Optional[float] = None,
    available_label: str = "available",
) -> str:
    mode = "PAPER" if paper else "LIVE"
    avail = float(cash if available is None else available)
    label = available_label or ("cash" if paper else "available margin")
    return (
        f"Balance ({mode})\n"
        f"cash=${float(cash):.2f}\n"
        f"{label}=${avail:.2f}\n"
        f"equity=${float(equity):.2f}"
    )


def recent_trades_from_ledger(
    db_path: Optional[str] = None,
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Last N BUY/SELL fills from paper_ledger.db (realized pnl on sells)."""
    import sqlite3

    path = (
        Path(db_path)
        if db_path
        else Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    )
    if not path.exists():
        return []
    lim = max(1, min(int(limit), 50))
    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE kind IN ('BUY','SELL','FILL') "
                "ORDER BY id DESC LIMIT ?",
                (lim,),
            )
        )
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recent_trades_from_ledger failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        try:
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_CT)
            when_s = when.strftime("%m/%d %H:%M CT")
        except (TypeError, ValueError, OSError):
            when_s = "?"
        side_u = (side or kind or "").upper()
        out.append(
            {
                "when": when_s,
                "symbol": str(symbol or "?"),
                "side": side_u,
                "qty": float(qty or 0),
                "price": float(price or 0),
                "notional": float(notional or 0),
                "pnl": float(pnl or 0),
            }
        )
    return out


def format_history_reply(trades: Sequence[Dict[str, Any]]) -> str:
    if not trades:
        return "History: (no fills in paper_ledger)"
    lines = [f"Last {len(trades)} trades:"]
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        pnl_s = f"-${abs(pnl):.2f}" if pnl < 0 else f"+${pnl:.2f}"
        lines.append(
            f"  {t.get('when','?')} {t.get('side','?')} {t.get('symbol','?')} "
            f"qty={_tg_fmt_qty(float(t.get('qty') or 0))} @ ${_tg_fmt_price(float(t.get('price') or 0))} "
            f"pnl={pnl_s}"
        )
    return "\n".join(lines)


_SECRET_REDACT_KEYS = (
    "BOT_TOKEN",
    "API_KEY",
    "API_SECRET",
    "SECRET",
    "PASSWORD",
    "PRIVATE_KEY",
    "BEARER",
    "AUTHORIZATION",
    "XAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "KRAKEN_API",
    "COINBASE",
)


def redact_secrets(text: str) -> str:
    """Best-effort scrub of tokens/keys from log snippets (Telegram-safe)."""
    import re

    s = text or ""
    s = re.sub(r"(?i)(bot\d{6,}:[A-Za-z0-9_-]{20,})", "[redacted-telegram-token]", s)
    s = re.sub(
        r"(?i)\b([A-Za-z0-9_-]*(?:api[_-]?key|api[_-]?secret|token|password|secret)[A-Za-z0-9_-]*)\s*[=:]\s*\S+",
        r"\1=[redacted]",
        s,
    )
    for key in _SECRET_REDACT_KEYS:
        s = re.sub(
            rf"(?i)\b{re.escape(key)}\b\s*[=:]\s*\S+",
            f"{key}=[redacted]",
            s,
        )
    return s


def resolve_instance_log_path(project_root: Optional[Any] = None) -> Optional[Any]:
    """Prefer Instance paper_loop.log, then logs/app.log."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    candidates = [
        root / "data" / "paper_loop.log",
        root / "logs" / "app.log",
        root / "data" / "app.log",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def read_tail_log_lines(
    path: Optional[Any] = None,
    *,
    project_root: Optional[Any] = None,
    n: int = 20,
    max_chars: int = 3500,
) -> str:
    """Last n lines of the instance log; secrets redacted; Telegram length-capped."""
    p = Path(path) if path else resolve_instance_log_path(project_root)
    if p is None or not p.exists():
        return "Logs: (no paper_loop.log or logs/app.log found for this instance)"
    try:
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = min(size, 64_000)
            fh.seek(max(0, size - block))
            raw = fh.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        tail = lines[-max(1, int(n)) :]
        body = "\n".join(redact_secrets(x) for x in tail)
        header = f"Last {len(tail)} lines ({p.name}):"
        out = header + "\n" + body
        if len(out) > max_chars:
            out = out[: max_chars - 20] + "\n…[truncated]"
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("read_tail_log_lines failed: %s", exc)
        return f"Logs: failed to read {p}: {exc}"


def parse_command(text: str) -> Optional[tuple]:
    """Parse `/cmd@bot args` → (cmd, args) or None if not a known command."""
    if not text:
        return None
    raw = text.strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    head = parts[0][1:]
    if "@" in head:
        head = head.split("@", 1)[0]
    cmd = head.strip().lower()
    if cmd not in KNOWN_COMMANDS:
        return None
    return cmd, parts[1:]



class TelegramCommandListener:
    """Long-poll getUpdates with flock single-poller lock."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        handlers: Dict[str, Handler],
        *,
        enabled: bool = True,
    ):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.handlers = handlers
        self.enabled = enabled
        self._offset = 0
        self._idle = False
        self._lock_fd = None
        self._client: Optional[httpx.AsyncClient] = None
        self._stop = asyncio.Event()

    @property
    def bot_id(self) -> str:
        if not self.token or ":" not in self.token:
            return "unknown"
        return self.token.split(":", 1)[0]

    @property
    def lock_path(self) -> str:
        return f"/tmp/cruzbot_tg_{self.bot_id}.lock"

    async def stop(self) -> None:
        self._stop.set()

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=60.0,
                headers={"User-Agent": "ApexSignalsNow/2.0 (+telegram)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:  # noqa: BLE001
                pass
            self._lock_fd = None

    async def _api(self, method: str, **params: Any) -> Any:
        if not self.token or self.token.startswith("YOUR_"):
            raise RuntimeError("Telegram token not configured")

        async def _do() -> Any:
            client = await self._client_get()
            url = f"https://api.telegram.org/bot{self.token}/{method}"
            r = await client.post(url, json=params)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return data.get("result")

        return await with_exponential_backoff(_do, label=f"telegram:{method}")

    async def set_my_commands(self) -> None:
        cmds = [{"command": c, "description": d[:40]} for c, d in BOT_COMMAND_SPECS]
        try:
            await self._api("setMyCommands", commands=cmds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("setMyCommands failed: %s", exc)

    async def send_message(self, text: str, chat_id: Optional[str] = None) -> None:
        cid = chat_id or self.chat_id
        if not cid:
            return
        try:
            await self._api("sendMessage", chat_id=cid, text=text[:4000])
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", exc)

    def _try_lock(self) -> bool:
        path = self.lock_path
        fd = open(path, "w", encoding="utf-8")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(str(os.getpid()))
            fd.flush()
            self._lock_fd = fd
            return True
        except BlockingIOError:
            fd.close()
            self._idle = True
            logger.warning(
                "Telegram lock held (%s) — listener idle (duplicate poller avoided)",
                path,
            )
            return False

    async def run(self) -> None:
        if not self.enabled:
            logger.info("Telegram commands disabled")
            return
        if not self.token or self.token.startswith("YOUR_"):
            logger.info("Telegram token placeholder — listener not started")
            return
        if not self._try_lock():
            # stay idle until stop
            while not self._stop.is_set():
                await asyncio.sleep(5)
            return
        await self.set_my_commands()
        logger.info("Telegram listener active (lock %s)", self.lock_path)
        while not self._stop.is_set():
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self._offset,
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                await asyncio.sleep(0.35)  # rate limit between polls
                for upd in updates or []:
                    self._offset = max(self._offset, int(upd["update_id"]) + 1)
                    await self._dispatch(upd)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(2.0)

    async def _dispatch(self, upd: Dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        cid = str(chat.get("id", ""))
        if self.chat_id and cid != self.chat_id:
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1:]
        if cmd not in KNOWN_COMMANDS:
            await self.send_message(f"Unknown command /{cmd}. Try /help", chat_id=cid)
            return
        handler = self.handlers.get(cmd)
        if not handler:
            await self.send_message(f"/{cmd} not wired yet", chat_id=cid)
            return
        try:
            reply = await handler(cmd, args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler /%s", cmd)
            reply = f"Error: {exc}"
        if reply:
            await self.send_message(str(reply), chat_id=cid)
