"""Telegram long-poll commands + flock single-poller + WF/profile helpers."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Tuple, Union

import httpx

from trading_bot.utils.entry_proximity import get_entry_threshold, make_progress_bar, progress_bar, proximity_bar
from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

STATUS_SYMBOLS_CALLBACK = "status:symbols"
STATUS_SYMBOLS_COLLAPSE = "status:symbols_collapse"


@dataclass
class TelegramReply:
    """Optional rich Telegram reply (inline keyboard / parse_mode)."""

    text: str
    reply_markup: Optional[Dict[str, Any]] = None
    parse_mode: Optional[str] = None


Handler = Callable[[str, List[str]], Awaitable[Union[str, TelegramReply, None]]]
CallbackHandler = Callable[[str], Awaitable[Union[str, TelegramReply, None]]]


def symbols_expand_keyboard(count: int) -> Dict[str, Any]:
    """Inline ▼ button under /status — expands the message in place."""
    n = int(count)
    label = f"▼ Symbols ({n})" if n else "▼ Symbols"
    return {"inline_keyboard": [[{"text": label, "callback_data": STATUS_SYMBOLS_CALLBACK}]]}


def symbols_collapse_keyboard(count: int) -> Dict[str, Any]:
    n = int(count)
    label = f"▲ Hide symbols ({n})" if n else "▲ Hide symbols"
    return {"inline_keyboard": [[{"text": label, "callback_data": STATUS_SYMBOLS_COLLAPSE}]]}


def status_with_symbols_button(text: str, *, symbol_count: int) -> TelegramReply:
    return TelegramReply(text=text, reply_markup=symbols_expand_keyboard(symbol_count))


def _status_compact_body(message_text: str) -> str:
    text = str(message_text or "")
    idx = text.find("—— Symbols")
    if idx >= 0:
        return text[:idx].rstrip()
    return text.rstrip()

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
    ("ban_risk", "API ban-risk hygiene score"),
    ("api_risk", "Alias for /ban_risk"),
    ("formula", "Formula health score + tips"),
    ("formula_score", "Alias for /formula"),
    ("phd", "PHD mode on/off/status (Tier-1 + WEAK gate)"),
    ("phd_mode", "Alias for /phd"),
    ("quant", "Session risk metrics + DD gate"),
    ("quant_metrics", "Alias for /quant"),
    ("live_safety", "LIVE safety + dead-man status"),
    ("future_pack", "Deferred pack readiness gates"),
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
ENV_KEY_PHD_MODE = "PHD_MODE"

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
    "taker_fee_rate",
    "maker_fee_rate",
    "circuit_breaker_enabled",
    "majors_only",
    "max_spread_pct",
    "trade_profile",
    "max_notional_per_trade_usd",
    "max_total_exposure_usd",
)

WF_SNAPSHOT_FIELDS = (
    "entry_threshold",
    "max_concurrent_positions",
    "min_tp_pct",
    "trail_fee_buffer_pct",
    "elite_fee_lock_arm_pct",
    "tp1_fraction",
    "stop_loss_profile",
    "taker_fee_rate",
    "maker_fee_rate",
    "circuit_breaker_enabled",
    "majors_only",
    "max_spread_pct",
    "trade_profile",
    "max_notional_per_trade_usd",
    "max_total_exposure_usd",
    "score",
    "n_trades",
    "expectancy",
    "ts",
)

_WF_TIER1_DEFAULTS: Dict[str, Any] = {
    "entry_threshold": 60.0,
    "max_concurrent_positions": 3,
    "min_tp_pct": 0.0305,
    "trail_fee_buffer_pct": 0.0125,
    "elite_fee_lock_arm_pct": 0.012,
    "tp1_fraction": 0.0,
    "stop_loss_profile": "medium",
    "taker_fee_rate": 0.008,
    "maker_fee_rate": 0.004,
    "circuit_breaker_enabled": True,
    "majors_only": True,
    "max_spread_pct": 0.002,
    "trade_profile": "medium",
    "max_notional_per_trade_usd": 750.0,
    "max_total_exposure_usd": 3000.0,
}

_WF_ENV_KEYS = {
    "entry_threshold": "ENTRY_THRESHOLD",
    "max_concurrent_positions": "MAX_CONCURRENT_POSITIONS",
    "min_tp_pct": "MIN_TP_PCT",
    "trail_fee_buffer_pct": "TRAIL_FEE_BUFFER_PCT",
    "elite_fee_lock_arm_pct": "ELITE_FEE_LOCK_ARM_PCT",
    "tp1_fraction": "TP1_FRACTION",
    "stop_loss_profile": "STOP_LOSS_PROFILE",
    "taker_fee_rate": "TAKER_FEE_RATE",
    "maker_fee_rate": "MAKER_FEE_RATE",
    "circuit_breaker_enabled": "CIRCUIT_BREAKER_ENABLED",
    "majors_only": "MAJORS_ONLY",
    "max_spread_pct": "MAX_SPREAD_PCT",
    "trade_profile": "TRADE_PROFILE",
    "max_notional_per_trade_usd": "MAX_NOTIONAL_PER_TRADE_USD",
    "max_total_exposure_usd": "MAX_TOTAL_EXPOSURE_USD",
}


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


def _wf_snapshot_paths(
    *, env_path: Optional[Path] = None, data_dir: Optional[Path] = None
) -> Tuple[Path, Path]:
    if data_dir is not None:
        root = Path(data_dir)
    elif env_path is not None:
        root = Path(env_path).resolve().parent / "data"
    else:
        root = Path(__file__).resolve().parents[1] / "data"
    return root / "wf_best_snapshot.json", root / "wf_last_snapshot.json"


def _load_json_dict(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _wf_snapshot_from_settings(
    settings: Any,
    *,
    score: Any = 0,
    n_trades: Any = 0,
    expectancy: Any = 0.0,
    ts: Any = None,
) -> Dict[str, Any]:
    values = dict(_WF_TIER1_DEFAULTS)
    for key in _WF_TIER1_DEFAULTS:
        value = getattr(settings, key, None)
        if value is not None:
            values[key] = value
    values.update(
        {
            "score": int(score or 0),
            "n_trades": int(n_trades or 0),
            "expectancy": float(expectancy or 0.0),
            "ts": float(ts or time.time()),
        }
    )
    return {key: values[key] for key in WF_SNAPSHOT_FIELDS}


def maybe_save_wf_best_snapshot(
    settings: Any,
    formula_snap: Dict[str, Any],
    *,
    env_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> bool:
    """Save a strictly better, fit WF pack; never auto-apply it."""
    if not bool(getattr(settings, "winning_formula", False)):
        return False
    band = str(formula_snap.get("band") or "").upper()
    metrics = formula_snap.get("metrics") or {}
    try:
        score = int(formula_snap.get("score") or 0)
        n_trades = int(metrics.get("closed_trades_n") or 0)
        expectancy = float(metrics.get("expectancy") or 0.0)
    except (TypeError, ValueError):
        return False
    if band == "WEAK" or not (n_trades >= 5 or (n_trades >= 3 and expectancy > 0)):
        return False
    best_path, last_path = _wf_snapshot_paths(env_path=env_path, data_dir=data_dir)
    prior = _load_json_dict(best_path)
    try:
        prior_score = int(prior.get("score", -1))
    except (TypeError, ValueError):
        prior_score = -1
    if score <= prior_score:
        return False
    payload = _wf_snapshot_from_settings(
        settings,
        score=score,
        n_trades=n_trades,
        expectancy=expectancy,
        ts=formula_snap.get("ts"),
    )
    _write_json_atomic(best_path, payload)
    _write_json_atomic(last_path, payload)
    logger.info("winning_formula: saved fit best snapshot score=%d n=%d", score, n_trades)
    return True


def note_wf_broken(
    settings: Any,
    reason: str,
    env_path: Optional[Path] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
) -> str:
    """Mark WF off after a knob change without restoring any prior knobs."""
    if not bool(getattr(settings, "winning_formula", False)):
        return ""
    object.__setattr__(settings, "winning_formula", False)
    env = os.environ if environ is None else environ
    env[ENV_KEY_WINNING_FORMULA] = "false"
    if env_path is not None:
        _persist_env(Path(env_path), {ENV_KEY_WINNING_FORMULA: "false"})
    message = f"winning_formula: OFF (knob changed: {reason})"
    logger.warning(message)
    return message


def _with_wf_broken(reply: str, broken: str) -> str:
    return f"{reply}\n{broken}" if broken else reply


def apply_runtime_entry_threshold(settings: Any, value: float) -> None:
    object.__setattr__(settings, "entry_threshold", float(value))
    os.environ["ENTRY_THRESHOLD"] = str(value)


# Telegram /set_spread: user enters percent (0.5 → 0.5%); Settings/.env store fraction.
SPREAD_PCT_MIN = 0.01  # 0.01%
SPREAD_PCT_MAX = 5.0  # 5.0%
SET_SPREAD_USAGE = (
    "Usage: /set_spread <pct> (e.g. /set_spread 0.5) — percent units, range "
    f"{SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}"
)


class SetSpreadError(ValueError):
    """Invalid /set_spread arguments — callers must not change state."""


def parse_set_spread_args(args: Sequence[str]) -> float:
    """Parse `/set_spread <pct>` → percent float (0.5 means 0.5%).

    Also accepts a single token like ``0.5%``. Raises SetSpreadError on failure.
    """
    if len(args) != 1:
        raise SetSpreadError(SET_SPREAD_USAGE)
    raw = str(args[0]).strip().replace(",", "")
    if raw.endswith("%"):
        raw = raw[:-1].strip()
    if not raw:
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}")
    try:
        pct = float(raw)
    except (TypeError, ValueError) as exc:
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}") from exc
    if pct != pct:  # NaN
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}")
    if pct < SPREAD_PCT_MIN or pct > SPREAD_PCT_MAX:
        raise SetSpreadError(
            f"Spread must be {SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}% inclusive. "
            f"Got {pct:g}%. {SET_SPREAD_USAGE}"
        )
    return pct


def parse_set_max_spread_alias(args: Sequence[str]) -> float:
    """Parse `/set MAX_SPREAD_PCT 0.5` or `/set MAX_SPREAD_PCT=0.5%` → percent."""
    if not args:
        raise SetSpreadError(
            "Usage: /set MAX_SPREAD_PCT <pct> or /set_spread <pct> "
            f"(e.g. /set_spread 0.5) — range {SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}"
        )
    tokens: list[str] = []
    for a in args:
        s = str(a).strip()
        if "=" in s:
            left, right = s.split("=", 1)
            if left:
                tokens.append(left)
            if right:
                tokens.append(right)
        else:
            tokens.append(s)
    if not tokens:
        raise SetSpreadError(SET_SPREAD_USAGE)
    key = tokens[0].strip().upper().replace("-", "_")
    if key not in ("MAX_SPREAD_PCT", "SPREAD", "SPREAD_PCT", "MAX_SPREAD"):
        raise SetSpreadError(
            f"Unknown /set key {tokens[0]!r}. Supported: MAX_SPREAD_PCT "
            f"(or use /set_spread <pct>)"
        )
    if len(tokens) != 2:
        raise SetSpreadError(
            "Usage: /set MAX_SPREAD_PCT <pct> (e.g. /set MAX_SPREAD_PCT 0.5 "
            "or /set MAX_SPREAD_PCT=0.5%)"
        )
    return parse_set_spread_args([tokens[1]])


def format_set_spread_reply(pct: float) -> str:
    """Telegram confirmation after successful /set_spread (pct is percent units)."""
    frac = float(pct) / 100.0
    frac_s = f"{frac:.10f}".rstrip("0").rstrip(".")
    return (
        f"✅ Spread cap updated to {float(pct):g}% "
        f"(MAX_SPREAD_PCT={frac_s}). "
        f"Entries wider than {float(pct):g}% mid-spread will be skipped."
    )


def apply_runtime_max_spread_pct(settings: Any, frac: float) -> None:
    object.__setattr__(settings, "max_spread_pct", float(frac))
    os.environ["MAX_SPREAD_PCT"] = str(frac)


def execute_set_spread(
    settings: Any,
    args: Sequence[str],
    *,
    env_path: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
    from_alias: bool = False,
) -> str:
    """Validate percent, persist fraction to .env MAX_SPREAD_PCT, mutate Settings."""
    pct = parse_set_max_spread_alias(args) if from_alias else parse_set_spread_args(args)
    frac = float(pct) / 100.0
    frac_s = f"{frac:.10f}".rstrip("0").rstrip(".")
    broken = note_wf_broken(settings, "max_spread_pct", env_path, environ=environ)
    apply_runtime_max_spread_pct(settings, frac)
    env_map = os.environ if environ is None else environ
    env_map["MAX_SPREAD_PCT"] = frac_s
    if env_path:
        _persist_env(Path(env_path), {"MAX_SPREAD_PCT": frac_s})
    return _with_wf_broken(format_set_spread_reply(pct), broken)



def execute_set_stop_loss(
    settings: Any,
    profile: str,
    *,
    env_path: Optional[Path] = None,
    signal_engine: Any = None,
    break_wf: bool = True,
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
    broken = note_wf_broken(settings, "stop_loss", env_path) if break_wf else ""
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
    reply = (
        f"{preset['emoji']} stop_loss → {preset['label']} "
        f"−{preset['sl_pct']*100:.2f}% / TP +{preset['tp_pct']*100:.2f}% "
        f"({preset['note']})"
    )
    return _with_wf_broken(reply, broken)



def normalize_stop_loss_profile(raw: Any) -> str:
    n = str(raw or "").strip().lower()
    if n in ("loose", "wide", "free"):
        return "free"
    if n in ("tight", "t", "red"):
        return "tight"
    if n in ("medium", "med", "m", "yellow", "bal", "balanced", ""):
        return "medium"
    if n in STOP_LOSS_PRESETS:
        return n
    return "medium"


def format_stop_loss_status_line(
    profile: str,
    *,
    effective_sl_pct: float | None = None,
    clamped: bool = False,
) -> str:
    """Compact /status line under Profile (optional WF BEAR effective %)."""
    try:
        name = normalize_stop_loss_profile(profile)
    except Exception:
        name = "medium"
    p = STOP_LOSS_PRESETS.get(name, STOP_LOSS_PRESETS["medium"])
    sl = float(effective_sl_pct) if effective_sl_pct is not None else float(p["sl_pct"])
    note = " (WF BEAR clamp)" if clamped else ""
    return f"stop_loss: {p['emoji']} {p['label']} −{sl * 100:.2f}%{note}"


def format_winning_formula_status_line(*, enabled: bool) -> str:
    return "winning_formula: ON 🚀" if enabled else "winning_formula: OFF"


def format_winning_formula_activated() -> str:
    return (
        "winning_formula: ON 🚀\n"
        "🚀 WINNING FORMULA ACTIVATED\n"
        "• Majors only: BTC/ETH/SOL/LINK/XCN\n"
        "• SL → 🟡 MEDIUM −1.50% / WF TP floor +3.05% (BEAR clamp −1.25% / ≤$6@$500)\n"
        "• Threshold 65% BEAR / 60% BULL · max 1 pos in BEAR\n"
        "• Tier-1 fees 0.80%/0.40% · HWM peak +1.20% → SL +1.25%\n"
        "• Time-exit maker ≥+1.25% · full exits (no partials)\n"
        "• Circuit: 3 consec losses · 45m gated auto-resume"
    )


def _wf_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return f"{value:g}"
    return str(value)


def execute_set_winning_formula(
    settings: Any,
    *,
    enabled: bool,
    env_path: Optional[Path] = None,
    environ: Optional[Dict[str, str]] = None,
    signal_engine: Any = None,
) -> str:
    env = environ if environ is not None else os.environ
    if enabled:
        if not bool(getattr(settings, "winning_formula", False)):
            saved = {k: getattr(settings, k, None) for k in _WF_SAVED_KEYS}
            object.__setattr__(settings, "_winning_formula_saved", saved)

        _best_path, last_path = _wf_snapshot_paths(env_path=env_path)
        prior = _load_json_dict(last_path)
        pack = dict(_WF_TIER1_DEFAULTS)
        for key in _WF_TIER1_DEFAULTS:
            if prior.get(key) is not None:
                pack[key] = prior[key]
        # These are invariant parts of the current Winning Formula setup.
        pack["majors_only"] = True
        pack["circuit_breaker_enabled"] = True
        pack["stop_loss_profile"] = "medium"

        execute_set_stop_loss(
            settings,
            "medium",
            env_path=env_path,
            signal_engine=signal_engine,
            break_wf=False,
        )
        for key, value in pack.items():
            object.__setattr__(settings, key, value)
        object.__setattr__(settings, "atr_bracket_tp_min_pct", float(pack["min_tp_pct"]))
        apply_runtime_entry_threshold(settings, float(pack["entry_threshold"]))
        object.__setattr__(settings, "winning_formula", True)

        updates: Dict[str, str] = {ENV_KEY_WINNING_FORMULA: "true"}
        for key, env_key in _WF_ENV_KEYS.items():
            updates[env_key] = _wf_env_value(pack[key])
        updates["ATR_BRACKET_TP_MIN_PCT"] = _wf_env_value(pack["min_tp_pct"])
        env.update(updates)
        if env_path:
            _persist_env(Path(env_path), updates)

        last_payload = _wf_snapshot_from_settings(
            settings,
            score=prior.get("score", 0),
            n_trades=prior.get("n_trades", 0),
            expectancy=prior.get("expectancy", 0.0),
            ts=prior.get("ts"),
        )
        _write_json_atomic(last_path, last_payload)
        return format_winning_formula_activated()

    # Explicit OFF may restore the pre-ON scratch. Auto-off uses note_wf_broken
    # instead and intentionally leaves all changed knobs in place.
    saved = getattr(settings, "_winning_formula_saved", None) or {}
    for key, value in saved.items():
        if value is not None:
            object.__setattr__(settings, key, value)
            if key == "entry_threshold":
                apply_runtime_entry_threshold(settings, float(value))
    object.__setattr__(settings, "winning_formula", False)
    env[ENV_KEY_WINNING_FORMULA] = "false"
    if env_path:
        _persist_env(Path(env_path), {ENV_KEY_WINNING_FORMULA: "false"})
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
    broken = note_wf_broken(settings, "trade_profile", env_path)
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
    reply = f"Profile → {key.upper()}. Threshold {preset['entry_threshold']:.0f}%"
    return _with_wf_broken(reply, broken)


def format_status_reply(
    *,
    paper_cash: float,
    paper_equity: float,
    wallet_b4: Optional[float] = None,
    positions: Sequence[Dict[str, Any]],
    paused: bool,
    strategy_mode: str,
    last_tick_age_seconds: Optional[float],
    paper: bool = True,
    symbols: Optional[Sequence[str]] = None,
    max_notional_per_trade: Optional[float] = None,
    max_total_exposure: Optional[float] = None,
    target_setup: Optional[str] = None,
    entry_proximity: Optional[Dict[str, Any]] = None,
    focus_symbol: Optional[str] = None,
    focus_price: Optional[float] = None,
    entry_threshold: Optional[float] = None,
    max_spread_pct: Optional[float] = None,
    trade_profile: Optional[str] = None,
    pid: Optional[int] = None,  # deprecated — ignored (kept for call-site compat)
    last_scan_latency_ms: Optional[float] = None,
    last_scan_pair_count: Optional[int] = None,
    market_state: Optional[str] = None,
    win_rate_pct: Optional[float] = None,
    session_wins: Optional[int] = None,
    session_losses: Optional[int] = None,
    symbol_mode: Optional[str] = None,
    universe_stocks: Optional[bool] = None,
    stock_count: Optional[int] = None,
    spot_long_only: bool = True,
    focus_block_reason: Optional[str] = None,
    tod_gate_enabled: Optional[bool] = None,
    tod_custom_lock: bool = False,
    stop_loss_profile: Optional[str] = None,
    stop_loss_effective_pct: Optional[float] = None,
    stop_loss_clamped: bool = False,
    winning_formula: bool = False,
    api_risk_line: Optional[str] = None,
    formula_score_line: Optional[str] = None,
    phd_mode: Optional[bool] = None,
    quant_line: Optional[str] = None,
    circuit_breaker_on: bool = False,
    circuit_breaker_consec_losses: int = 0,
    caps_locked: bool = False,
    majors_only: Optional[bool] = None,
    majors_symbols: Optional[Sequence[str]] = None,
) -> str:
    """Unified /status reply (Apex Signals Now) — same layout on Kraken + Coinbase.

    Field order (operator layout):
      Apex Signals Now {PAPER|LIVE} status
      Last Scan Latency / last_tick_age / circuit breaker
      cash=… / equity=… (+ WR)
      pause=… (only when paused)
      Market: …
      caps=…
      (blank)
      winning_formula / Profile / tod_custom / stop_loss / Threshold / Spread
      Target Setup / Entry Proximity / focus
      positions
      Universe / Symbols
    """
    from trading_bot.utils.entry_proximity import get_entry_threshold, make_progress_bar

    _ = pid  # intentionally unused
    thresh_pct = int(round(float(
        entry_threshold if entry_threshold is not None else get_entry_threshold()
    )))

    nl = chr(10)
    mode = "PAPER" if paper else "LIVE"
    pause_s = "PAUSED (no new buys)" if paused else "running"
    if last_tick_age_seconds is None:
        tick_s = "n/a"
    else:
        tick_s = f"{float(last_tick_age_seconds):.1f}s"

    # Win rate block — big on the right of cash/equity/pause (paper session)
    wr_big = ""
    wr_sub = ""
    if paper:
        w = int(session_wins or 0)
        l = int(session_losses or 0)
        if win_rate_pct is not None:
            wr_big = f"WR {float(win_rate_pct):.0f}%"
            wr_sub = f"{w}W/{l}L"
        elif (w + l) > 0:
            wr_big = f"WR {100.0 * w / (w + l):.0f}%"
            wr_sub = f"{w}W/{l}L"
        else:
            wr_big = "WR —"
            wr_sub = "0W/0L"

    def _row(left: str, right: str = "", width: int = 36) -> str:
        if not right:
            return left
        # keep right edge readable on mobile Telegram
        gap = max(2, width - len(left) - len(right))
        return f"{left}{' ' * gap}{right}"

    # --- header + scan health (top) ---
    lines = [
        f"Apex Signals Now {mode} status",
        "",
    ]
    _ = strategy_mode  # kept for call-site compat; not shown on /status
    if last_scan_latency_ms is not None:
        try:
            lat = max(0.0, float(last_scan_latency_ms))
            pairs = int(last_scan_pair_count) if last_scan_pair_count is not None else 0
            if pairs <= 0 and symbols:
                pairs = len(list(symbols))
            if abs(lat - round(lat)) < 0.05:
                lat_s = f"{int(round(lat))}ms"
            else:
                lat_s = f"{lat:.1f}ms"
            lines.append(f"Last Scan Latency: {lat_s} across {pairs} pairs")
        except (TypeError, ValueError):
            pass
    lines.append(f"last_tick_age={tick_s}")
    lines.append("")  # space before api risk / circuit breaker
    if api_risk_line:
        lines.append(str(api_risk_line).strip())
        lines.append("")
    if formula_score_line:
        lines.append(str(formula_score_line).strip())
        lines.append("")
    if phd_mode is not None:
        lines.append(format_phd_status_line(enabled=bool(phd_mode)))
        lines.append("")
    if quant_line:
        lines.append(str(quant_line).strip())
        lines.append("")
    cb_state = "ON" if circuit_breaker_on else "OFF"
    try:
        cl = max(0, int(circuit_breaker_consec_losses))
    except (TypeError, ValueError):
        cl = 0
    # Telegram bots cannot set font color; 🔴 is the supported "red" cue
    lines.append(
        f"(⚠️☣️circuit breaker ☣️⚠️) {cb_state} · {cl} consecutive loss"
        f"{'' if cl == 1 else 'es'}"
    )
    lines.append("")
    lines.append("")

    # --- cash / equity / WR (WR on its own line — Telegram fonts break right-align) ---
    lines.append(f"cash=${float(paper_cash):.2f}")
    lines.append(f"equity=${float(paper_equity):.2f}")
    if wallet_b4 is not None:
        try:
            lines.append(f"💳 Wallet B4=${float(wallet_b4):.2f}")
        except (TypeError, ValueError):
            pass
    if paper and (wr_big or wr_sub):
        wr_bits = [b for b in (wr_big, wr_sub) if b]
        lines.append(" · ".join(wr_bits) if wr_bits else "WR —")
    if paused:
        lines.append(f"pause={pause_s}")
    lines.append("")

    # --- market + caps ---
    ms_raw = str(market_state or "").strip()
    if ms_raw:
        ms_line = ms_raw
        msu = ms_raw.upper()
        if ("BEAR" in msu or "SHORT" in msu) and "BIAS=" not in msu:
            ms_line = f"{ms_raw} bias=SHORT"
        lines.append(f"Market: {ms_line}")
    trade_c = (
        f"${float(max_notional_per_trade):.0f}"
        if max_notional_per_trade is not None
        else "?"
    )
    exp_c = (
        f"${float(max_total_exposure):.0f}"
        if max_total_exposure is not None
        else "?"
    )
    caps_line = f"caps={trade_c}/trade {exp_c} exposure"
    if caps_locked:
        caps_line += " 🔒"
    lines.append(caps_line)
    lines.append("")  # space before profile block

    # --- profile block ---
    lines.append(format_winning_formula_status_line(enabled=bool(winning_formula)))
    prof = (trade_profile or "").strip().lower()
    if prof in ("aggressive", "medium", "low"):
        lines.append(f"Profile: {prof.upper()}")
    elif prof:
        lines.append(f"Profile: {prof.upper()}")
    else:
        lines.append("Profile: MEDIUM")
    if tod_gate_enabled is None:
        tod_s = "n/a"
    else:
        tod_s = "ON" if bool(tod_gate_enabled) else "OFF"
        if tod_custom_lock:
            tod_s += " 🔒"
    lines.append(f"tod_custom: {tod_s}")
    lines.append(format_stop_loss_status_line(
        stop_loss_profile or "medium",
        effective_sl_pct=stop_loss_effective_pct,
        clamped=bool(stop_loss_clamped),
    ))
    lines.append(f"Threshold: {thresh_pct}%")
    if max_spread_pct is not None:
        try:
            spread_pct_display = float(max_spread_pct) * 100.0
            spread_s = f"{spread_pct_display:.4f}".rstrip("0").rstrip(".")
            lines.append(f"Spread cap: {spread_s}%")
        except (TypeError, ValueError):
            lines.append("Spread cap: n/a")
    else:
        lines.append("Spread cap: n/a")



    prox = entry_proximity if isinstance(entry_proximity, dict) else None

    # Target Setup: LONG or WAIT only (Kraken spot — never SHORT).
    direction = None
    if prox is not None:
        direction = str(prox.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT", "WAIT"):
        raw = (target_setup or "").strip().upper()
        if "SPOT MODE" in raw and "LONG" in raw:
            direction = "LONG"
        elif raw in ("LONG", "SHORT", "WAIT"):
            direction = raw
        elif "LONG" in raw.split():
            direction = "LONG"
        else:
            direction = "WAIT"
    if direction == "SHORT":
        direction = "WAIT"
    try:
        thr = float(entry_threshold) if entry_threshold is not None else float(get_entry_threshold())
    except Exception:
        thr = 60.0
    ms = str(market_state or "").upper()
    bearish = ("BEAR_CHOP" in ms or "BIAS=SHORT" in ms or ms == "SHORT")
    if spot_long_only and bearish:
        thr = thr * 1.10
    try:
        score_now = float((prox or {}).get("score") or 0.0) if prox else 0.0
    except (TypeError, ValueError):
        score_now = 0.0
    if direction == "LONG" and score_now < thr:
        direction = "WAIT"
    if direction not in ("LONG", "WAIT"):
        direction = "WAIT"

    lines.append("")
    if direction == "LONG" and spot_long_only:
        lines.append("Target Setup: LONG (Spot Mode)")
    else:
        lines.append(f"Target Setup: {direction}")

    if prox is not None:
        try:
            score = float(prox.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
    else:
        score = 0.0
    score = max(0.0, min(100.0, score))
    raw_bar = prox.get("bar") if prox is not None else None
    if isinstance(raw_bar, str) and raw_bar.startswith("[") and raw_bar.endswith("]"):
        bar = raw_bar
    elif isinstance(raw_bar, str) and raw_bar:
        bar = f"[{raw_bar}]"
    else:
        bar = f"[{make_progress_bar(score)}]"
    lines.append(f"Entry Proximity: {bar} {score:.1f}%")

    focus_sym = focus_symbol
    focus_px = focus_price
    if prox is not None:
        if not focus_sym:
            focus_sym = prox.get("symbol")
        if focus_px is None:
            focus_px = prox.get("price")
    if focus_sym:
        try:
            px = float(focus_px) if focus_px is not None else None
        except (TypeError, ValueError):
            px = None
        if px is not None and px > 0:
            focus_line = f"focus={focus_sym} @ ${_tg_fmt_price(px)}"
        else:
            focus_line = f"focus={focus_sym}"
        if direction == "WAIT" and score >= 100.0 - 1e-9:
            reason = (focus_block_reason or "").strip() or "entries frozen"
            focus_line += f" [BLOCKED: {reason}]"
        lines.append(focus_line)
    else:
        lines.append("focus=n/a")

    lines.append("")
    if not positions:

        lines.append("positions: (none)")
    else:
        lines.append(f"positions ({len(positions)}):")
        for p in positions:
            # Each open position is a self-contained block: symbol + own bar +
            # entry + PnL. Never share bar/score state across symbols.
            sym = p.get("symbol") or "?"
            try:
                qty = float(p.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            entry = p.get("avg_entry_price")
            mv = p.get("market_value")
            upl = p.get("unrealized_pl")
            side = str(p.get("side") or "long").lower()
            tp = p.get("take_profit")
            mark = p.get("mark_price")
            if mark is None and entry is not None and qty and mv is not None:
                try:
                    mark = float(mv) / abs(float(qty))
                except (TypeError, ValueError, ZeroDivisionError):
                    mark = None

            # Unrealized PnL $ / %
            try:
                upl_f = float(upl) if upl is not None else None
            except (TypeError, ValueError):
                upl_f = None
            if upl_f is None and entry is not None and mark is not None and qty:
                try:
                    e = float(entry)
                    m = float(mark)
                    if side == "short":
                        upl_f = (e - m) * abs(float(qty))
                    else:
                        upl_f = (m - e) * abs(float(qty))
                except (TypeError, ValueError):
                    upl_f = None
            pnl_pct = None
            try:
                if upl_f is not None and entry is not None and abs(float(qty)) > 0:
                    cost = abs(float(entry) * float(qty))
                    if cost > 0:
                        pnl_pct = 100.0 * float(upl_f) / cost
            except (TypeError, ValueError):
                pnl_pct = None
            if pnl_pct is None:
                try:
                    raw_pct = p.get("pnl_pct")
                    if raw_pct is not None:
                        pnl_pct = float(raw_pct)
                except (TypeError, ValueError):
                    pnl_pct = None

            # Progress = current_pnl_pct / tp_target_pct * 100 (tp floor 2%).
            # 100% ≈ +2% gross on a $500 ticket (~+$10 before fees).
            progress = None
            sl = p.get("stop_loss")
            try:
                e = float(entry) if entry is not None else 0.0
                m = float(mark) if mark is not None else 0.0
                if e > 0 and m > 0:
                    if side == "short":
                        cur_pnl_pct = (e - m) / e
                    else:
                        cur_pnl_pct = (m - e) / e
                    tp_target_pct = 0.02
                    if tp is not None and abs(float(tp) - e) > 1e-12:
                        if side == "short":
                            tp_target_pct = max(0.02, (e - float(tp)) / e)
                        else:
                            tp_target_pct = max(0.02, (float(tp) - e) / e)
                    progress = 100.0 * cur_pnl_pct / tp_target_pct
            except (TypeError, ValueError, ZeroDivisionError):
                progress = None
            if progress is None and pnl_pct is not None:
                progress = 100.0 * (float(pnl_pct) / 100.0) / 0.02
            if progress is None:
                progress = 0.0
            progress = max(0.0, min(100.0, float(progress)))
            bar = f"[{make_progress_bar(progress)}]"

            if entry is None:
                entry_s = "?"
            else:
                e = float(entry)
                entry_s = f"${_tg_fmt_price(e)}"
            mv_s = f"${float(mv):.2f}" if mv is not None else "?"
            if upl_f is None:
                pnl_s = "n/a"
            else:
                money = f"+${upl_f:.2f}" if upl_f >= 0 else f"-${abs(upl_f):.2f}"
                if pnl_pct is None:
                    pnl_s = money
                else:
                    pnl_s = f"{money} ({pnl_pct:+.2f}%)"
            side_s = "SHORT" if side == "short" else "LONG"

            if mark is None:
                live_s = "?"
            else:
                m = float(mark)
                live_s = f"${_tg_fmt_price(m)}"

            lines.append("")
            lines.append(f"  {sym}  ({side_s})")
            lines.append(f"  Progress: {bar} {progress:.1f}%")
            lines.append(f"  live={live_s}")
            lines.append(f"  entry={entry_s}  pnl={pnl_s}")
            lines.append(f"  qty={_tg_fmt_qty(qty)}  mv={mv_s}")

    # Universe line + pair list (always last; keep elite regime / WR layout intact)
    lines.append("")
    sym_list = [str(s) for s in symbols] if symbols is not None else []
    lines.append(format_universe_line(symbol_mode, len(sym_list), stocks_enabled=universe_stocks, stocks_count=stock_count))
    if universe_stocks is None:
        # Preserve the legacy compact footer for callers that do not request
        # stock-universe metadata (older integrations/tests).
        lines.append("symbols=" + ",".join(sym_list))
    else:
        # Keep /status short — full list opens via ▼ Symbols inline button.
        n = len(sym_list)
        if n:
            lines.append(f"Symbols: {n} pairs · tap ▼ to expand")
        else:
            lines.append("Symbols: (none) · tap ▼ to expand")

    if majors_only is not None:
        if majors_only:
            maj_list = [str(s) for s in (majors_symbols or []) if s]
            if maj_list:
                lines.append("majors_only: ON (" + ",".join(maj_list) + ")")
            else:
                lines.append("majors_only: ON")
        else:
            lines.append("majors_only: OFF")

    return nl.join(lines)


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


# /majors — filter scan universe to BTC/ETH/SOL/LINK/XCN (Onyxcoin)
MAJORS_USAGE = "Usage: /majors [on|off|status]"
MAJORS_DEFAULT_SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "XCN-USD")


def parse_majors_args(args: Sequence[str]) -> Optional[str]:
    """Return on|off|status, or None if invalid. Bare /majors → status."""
    if not args:
        return "status"
    raw = str(args[0]).strip().lower()
    if raw in ("on", "1", "true", "enable", "enabled"):
        return "on"
    if raw in ("off", "0", "false", "disable", "disabled"):
        return "off"
    if raw in ("status", "stat", "show", "?"):
        return "status"
    return None


def format_majors_status(
    *,
    enabled: bool,
    symbols: Optional[Sequence[str]] = None,
) -> str:
    state = "ON" if enabled else "OFF"
    maj = [str(s) for s in (symbols or MAJORS_DEFAULT_SYMBOLS) if s]
    if enabled and maj:
        return f"majors_only: {state} (" + ",".join(maj) + ")"
    return f"majors_only: {state}"


def execute_set_majors(
    settings: Any,
    enabled: bool,
    *,
    env_path: Optional[Path] = None,
    majors_symbols: Optional[Sequence[str]] = None,
) -> str:
    """Persist MAJORS_ONLY and mutate runtime Settings."""
    object.__setattr__(settings, "majors_only", bool(enabled))
    os.environ["MAJORS_ONLY"] = "true" if enabled else "false"
    if env_path is not None:
        _persist_env(Path(env_path), {"MAJORS_ONLY": "true" if enabled else "false"})
    return format_majors_status(enabled=bool(enabled), symbols=majors_symbols)


# /phd — Professional High Discipline ops pack + WEAK formula soft entry gate
PHD_USAGE = "Usage: /phd [on|off|status]"


def parse_phd_args(args: Sequence[str]) -> Optional[str]:
    """Return on|off|status, or None if invalid. Bare /phd → status."""
    if not args:
        return "status"
    raw = str(args[0]).strip().lower()
    if raw in ("on", "1", "true", "enable", "enabled"):
        return "on"
    if raw in ("off", "0", "false", "disable", "disabled"):
        return "off"
    if raw in ("status", "stat", "show", "?"):
        return "status"
    return None


def format_phd_status_line(*, enabled: bool) -> str:
    return f"phd: {'ON' if enabled else 'OFF'}"


def phd_weak_entry_gate(
    *,
    phd_mode: bool,
    formula_band: Optional[str] = None,
    formula_score: Optional[int] = None,
) -> Optional[str]:
    """If PHD on and formula band WEAK, return focus_blocked reason (entries only).

    Does not mutate settings / knobs. Exits remain caller's responsibility.
    """
    if not phd_mode:
        return None
    band = str(formula_band or "").strip().upper()
    if band != "WEAK":
        return None
    try:
        n = int(formula_score) if formula_score is not None else 0
    except (TypeError, ValueError):
        n = 0
    return f"phd: formula WEAK ({n}/100) — see /formula"


def clear_phd_focus_block(ops: Any) -> bool:
    """Clear ops.focus_blocked when it is a phd-prefixed soft gate. Returns True if cleared."""
    if ops is None:
        return False
    try:
        cur = str(getattr(ops, "focus_blocked", "") or "")
    except Exception:  # noqa: BLE001
        return False
    if cur.startswith("phd:"):
        try:
            ops.focus_blocked = ""
        except Exception:  # noqa: BLE001
            return False
        return True
    return False


def format_phd_on_reply(*, majors_symbols: Optional[Sequence[str]] = None) -> str:
    maj = [str(s) for s in (majors_symbols or MAJORS_DEFAULT_SYMBOLS) if s]
    maj_s = ",".join(maj) if maj else "BTC-USD,ETH-USD,SOL-USD,LINK-USD,XCN-USD"
    return (
        "🎓 PHD mode ON (Professional High Discipline)\n"
        "• winning_formula: ON (Tier-1)\n"
        "• Profile: MEDIUM (label; WF knobs kept)\n"
        "• stop_loss: MEDIUM\n"
        "• circuit_breaker: ON\n"
        f"• majors_only: ON ({maj_s})\n"
        "• Soft gate: block NEW entries when formula WEAK "
        "(exits OK; /formula tips not auto-applied)"
    )


def format_phd_off_reply(*, winning_formula_left: bool) -> str:
    wf = "ON" if winning_formula_left else "OFF"
    return (
        f"PHD mode OFF\n"
        f"• phd_mode cleared; WEAK soft gate disabled\n"
        f"• winning_formula left as-is ({wf}) — not auto-turned off"
    )


def execute_set_phd_mode(
    settings: Any,
    *,
    enabled: bool,
    env_path: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
    ops: Any = None,
    signal_engine: Any = None,
    majors_symbols: Optional[Sequence[str]] = None,
) -> str:
    """Apply or clear PHD pack. ON forces Tier-1 helpers; OFF only clears phd_mode + gate."""
    env = environ if environ is not None else os.environ
    if enabled:
        # Tier-1 stack via existing helpers (WF also forces SL medium + CB on)
        execute_set_winning_formula(
            settings,
            enabled=True,
            env_path=env_path,
            environ=env if isinstance(env, dict) else None,
            signal_engine=signal_engine,
        )
        # Label profile medium without execute_set_trade_profile (that clears WF)
        object.__setattr__(settings, "trade_profile", "medium")
        env["TRADE_PROFILE"] = "medium"
        execute_set_stop_loss(
            settings,
            "medium",
            env_path=env_path,
            signal_engine=signal_engine,
            break_wf=False,
        )
        execute_set_circuity_breaker(
            settings,
            enabled=True,
            env_path=env_path,
            environ=env,
            ops=ops,
            consec=0,
            tripped=False,
            break_wf=False,
        )
        execute_set_majors(
            settings,
            True,
            env_path=env_path,
            majors_symbols=majors_symbols,
        )
        object.__setattr__(settings, "phd_mode", True)
        env[ENV_KEY_PHD_MODE] = "true"
        updates = {
            ENV_KEY_PHD_MODE: "true",
            "TRADE_PROFILE": "medium",
        }
        if env_path:
            _persist_env(Path(env_path), updates)
        return format_phd_on_reply(majors_symbols=majors_symbols)

    object.__setattr__(settings, "phd_mode", False)
    env[ENV_KEY_PHD_MODE] = "false"
    if env_path:
        _persist_env(Path(env_path), {ENV_KEY_PHD_MODE: "false"})
    clear_phd_focus_block(ops)
    return format_phd_off_reply(
        winning_formula_left=bool(getattr(settings, "winning_formula", False))
    )


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
    break_wf: bool = True,
) -> str:
    """Persist CIRCUIT_BREAKER_ENABLED; losses always counted; auto-pause follows flag."""
    env = environ if environ is not None else os.environ
    broken = (
        note_wf_broken(settings, "circuit_breaker_enabled", env_path, environ=env)
        if break_wf
        else ""
    )
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
    reply = format_circuity_breaker_status(
        enabled=flag, consec=int(consec or 0), tripped=bool(tripped) and flag
    )
    return _with_wf_broken(reply, broken)


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



def day_trades_from_ledger(
    db_path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Build performance_report trade rows from today's SELL fills in paper_ledger."""
    import sqlite3

    path = Path(db_path) if db_path else Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    if not path.exists():
        return []
    now_dt = now or datetime.now(_CT)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_CT)
    else:
        now_dt = now_dt.astimezone(_CT)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = day_start.timestamp()

    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE ts >= ? AND kind IN ('BUY','SELL','FILL') ORDER BY id",
                (start_ts,),
            )
        )
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("day_trades_from_ledger failed: %s", exc)
        return []

    buys_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    trades: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        side_u = (side or "").upper()
        kind_u = (kind or "").upper()
        is_buy = kind_u == "BUY" or side_u == "BUY"
        is_sell = kind_u == "SELL" or side_u == "SELL"
        try:
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            when = datetime.now(timezone.utc)
        sym = str(symbol or "")
        if is_buy and not is_sell:
            buys_by_sym.setdefault(sym, []).append(
                {
                    "when": when,
                    "cost": float(notional or 0) or (float(qty or 0) * float(price or 0)),
                    "price": float(price or 0),
                    "qty": float(qty or 0),
                }
            )
            continue
        if is_sell:
            cost = 0.0
            pending = buys_by_sym.get(sym) or []
            if pending:
                b = pending.pop(0)
                cost = float(b.get("cost") or 0)
            exit_n = float(notional or 0) or (float(qty or 0) * float(price or 0))
            trades.append(
                {
                    "when": when,
                    "symbol": sym,
                    "cost": cost,
                    "exit": exit_n,
                    "pnl": float(pnl or 0),
                }
            )
    return trades


def format_performance_report(
    *,
    trades: Sequence[Dict[str, Any]],
    cash: float,
    equity: float,
    paper: bool = True,
    now: Optional[datetime] = None,
) -> str:
    """📊 CRUZBOT PERFORMANCE REPORT — production /pnl table layout."""
    now_dt = now or datetime.now(_CT)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_CT)
    else:
        now_dt = now_dt.astimezone(_CT)
    date_s = now_dt.strftime("%m/%d/%Y")
    sep = "---------------------------------"
    lines = [
        "📊 CRUZBOT PERFORMANCE REPORT",
        f"Date: {date_s}",
        sep,
        "Date/Time | Symbol | Cost | Exit | Net P&L",
        sep,
    ]

    def money(x: float) -> str:
        return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"

    def money_plain(x: float) -> str:
        return f"${x:,.2f}"

    day_net = 0.0
    wins = 0
    losses = 0
    for t in trades:
        when = t.get("when")
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when_ct = when.astimezone(_CT)
            try:
                when_s = when_ct.strftime("%-I:%M %p")
            except ValueError:
                when_s = when_ct.strftime("%I:%M %p").lstrip("0")
        else:
            when_s = str(when or "?")
        sym = str(t.get("symbol") or "?")
        cost = float(t.get("cost") or 0)
        exit_v = float(t.get("exit") or 0)
        pnl = float(t.get("pnl") or 0)
        day_net += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        lines.append(
            f"{when_s:<8} | {sym:<8} | {money_plain(cost)} | {money_plain(exit_v)} | {money(pnl)}"
        )
    lines.append(sep)
    total = len(trades)
    if total:
        wr = 100.0 * wins / total
        wr_s = f"{wr:.0f}% ({wins}W / {losses}L)"
    else:
        wr_s = "— (0W / 0L)"
    bank_label = "Paper Bankroll" if paper else "Live Bankroll"
    lines.append(f"• Total Trades: {total}")
    lines.append(f"• Win Rate: {wr_s}")
    lines.append(f"• Day Net P&L: {money(day_net)}")
    lines.append(f"• {bank_label}: ${float(cash):,.2f} | ${float(equity):,.2f}")
    return "\n".join(lines)


def closed_trades_to_day_rows(
    closed: Sequence[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Map paper-book closed_trades into performance_report rows (today only)."""
    now_dt = now or datetime.now(_CT)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_CT)
    else:
        now_dt = now_dt.astimezone(_CT)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    out: List[Dict[str, Any]] = []
    for t in closed or []:
        raw_when = t.get("closed_at") or t.get("when") or t.get("ts")
        when: Optional[datetime] = None
        if isinstance(raw_when, datetime):
            when = raw_when
        elif isinstance(raw_when, (int, float)):
            try:
                when = datetime.fromtimestamp(float(raw_when), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                when = None
        elif isinstance(raw_when, str) and raw_when:
            try:
                when = datetime.fromisoformat(raw_when.replace("Z", "+00:00"))
            except ValueError:
                when = None
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when_ct = when.astimezone(_CT)
        if when_ct < day_start:
            continue
        qty = float(t.get("qty") or 0)
        entry = float(t.get("entry") or 0)
        exit_px = float(t.get("exit") or t.get("price") or 0)
        cost = abs(qty * entry)
        exit_n = abs(qty * exit_px)
        out.append(
            {
                "when": when,
                "symbol": str(t.get("symbol") or "?"),
                "cost": cost,
                "exit": exit_n,
                "pnl": float(t.get("pnl") or 0),
            }
        )
    return out



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
        reason = str(t.get("reason") or "").strip()
        peak = t.get("peak_upl_pct")
        extra = ""
        if reason:
            extra += f" {reason}"
        if peak is not None:
            try:
                extra += f" peak={float(peak):+.2f}%"
            except (TypeError, ValueError):
                pass
        lines.append(
            f"  {t.get('when','?')} {t.get('side','?')} {t.get('symbol','?')} "
            f"qty={_tg_fmt_qty(float(t.get('qty') or 0))} @ ${_tg_fmt_price(float(t.get('price') or 0))} "
            f"pnl={pnl_s}{extra}"
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
    """Long-poll getUpdates with flock single-poller lock + inline symbol expand."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        handlers: Dict[str, Handler],
        *,
        enabled: bool = True,
        callback_handlers: Optional[Dict[str, CallbackHandler]] = None,
    ):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.handlers = handlers
        self.callback_handlers = dict(callback_handlers or {})
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
        await self._send_reply(text, chat_id=chat_id)

    async def _send_reply(
        self,
        reply: Union[str, TelegramReply, None],
        *,
        chat_id: Optional[str] = None,
    ) -> None:
        cid = chat_id or self.chat_id
        if not cid or reply is None:
            return
        if isinstance(reply, TelegramReply):
            text = reply.text or ""
            reply_markup = reply.reply_markup
            parse_mode = reply.parse_mode
        else:
            text = str(reply or "")
            reply_markup = None
            parse_mode = None
        if not text and not reply_markup:
            return
        # Split long replies so the full symbol list stays scrollable.
        chunks: List[str] = []
        body = text or ""
        while body:
            chunks.append(body[:3500])
            body = body[3500:]
        if not chunks:
            chunks = [""]
        try:
            for i, chunk in enumerate(chunks):
                payload: Dict[str, Any] = {
                    "chat_id": cid,
                    "text": (chunk or "(empty)")[:4000],
                    "disable_web_page_preview": True,
                }
                if parse_mode and i == 0:
                    payload["parse_mode"] = parse_mode
                # Attach ▼ button only on the first (status) chunk.
                if reply_markup and i == 0:
                    payload["reply_markup"] = reply_markup
                await self._api("sendMessage", **payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", exc)

    async def _answer_callback(self, callback_query_id: str, *, text: str = "") -> None:
        if not callback_query_id:
            return
        try:
            await self._api(
                "answerCallbackQuery",
                callback_query_id=callback_query_id,
                text=(text or "")[:200],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("answerCallbackQuery failed: %s", exc)

    async def _edit_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": (text or "")[:4090],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._api("editMessageText", **payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("editMessageText failed: %s", exc)

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
        # Never log full bot token URLs
        logging.getLogger("httpx").setLevel(logging.WARNING)
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
        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return
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
            await self._send_reply(f"Unknown command /{cmd}. Try /help", chat_id=cid)
            return
        handler = self.handlers.get(cmd)
        if not handler:
            await self._send_reply(f"/{cmd} not wired yet", chat_id=cid)
            return
        try:
            reply = await handler(cmd, args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler /%s", cmd)
            reply = f"Error: {exc}"
        if reply:
            await self._send_reply(reply, chat_id=cid)

    async def _handle_callback(self, cb: Dict[str, Any]) -> None:
        cq_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = msg.get("message_id")
        if self.chat_id and chat_id != self.chat_id:
            await self._answer_callback(cq_id, text="Unauthorized")
            return

        if data in (STATUS_SYMBOLS_CALLBACK, STATUS_SYMBOLS_COLLAPSE) and message_id is not None:
            compact = _status_compact_body(str(msg.get("text") or ""))
            n_line = 0
            for line in compact.splitlines():
                if line.startswith("Symbols:") and "pairs" in line:
                    try:
                        n_line = int(line.split("Symbols:", 1)[1].split("pairs", 1)[0].strip())
                    except ValueError:
                        n_line = 0
                    break
            if data == STATUS_SYMBOLS_COLLAPSE:
                await self._answer_callback(cq_id, text="Collapsed")
                await self._edit_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=compact,
                    reply_markup=symbols_expand_keyboard(n_line),
                )
                return
            handler = self.callback_handlers.get(STATUS_SYMBOLS_CALLBACK)
            if handler is None:
                await self._answer_callback(cq_id, text="Unknown button")
                return
            try:
                symbols_body = await handler(data)
            except Exception as exc:  # noqa: BLE001
                logger.exception("telegram callback %s failed", data)
                await self._answer_callback(cq_id, text="Error")
                await self._send_reply(f"Button error: {exc}", chat_id=chat_id)
                return
            symbols_text = (
                symbols_body.text if isinstance(symbols_body, TelegramReply) else str(symbols_body or "")
            )
            expanded = compact.rstrip() + "\n\n—— Symbols ——\n" + symbols_text.strip()
            if len(expanded) > 4090:
                expanded = expanded[:4080] + "\n… (truncated — /symbols)"
            await self._answer_callback(cq_id, text="Expanded")
            await self._edit_message(
                chat_id=chat_id,
                message_id=int(message_id),
                text=expanded,
                reply_markup=symbols_collapse_keyboard(n_line),
            )
            return

        handler = self.callback_handlers.get(data)
        if handler is None:
            await self._answer_callback(cq_id, text="Unknown button")
            return
        try:
            reply = await handler(data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram callback %s failed", data)
            await self._answer_callback(cq_id, text="Error")
            await self._send_reply(f"Button error: {exc}", chat_id=chat_id)
            return
        await self._answer_callback(cq_id, text="OK")
        if reply:
            await self._send_reply(reply, chat_id=chat_id)
