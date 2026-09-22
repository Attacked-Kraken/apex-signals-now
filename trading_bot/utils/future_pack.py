"""Future Pack notepad — readiness gates only. Never auto-edits trading knobs.

A formula score in the 70s is a deadline/readiness *goal*, not permission
to implement risky features. "v" is not a greenlight.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from trading_bot.utils.formula_score import (
    _expectancy_from_closes,
    band_for_score,
    band_icon,
    default_formula_memory_path,
)

STATUS_WAIT = "WAIT"
STATUS_READY = "READY"
STATUS_DONE = "DONE"

URGENT_SCORE = 65  # warn when approaching the 70s with WAIT prerequisites

# Canonical deferred items + the two live-safety outcomes of this ship.
ITEM_SPECS: List[Dict[str, str]] = [
    {
        "id": "regime_scaled_size",
        "title": "Regime-scaled size",
        "gate": "User explicit greenlight OR clean paper week. NOT triggered by \"v\".",
    },
    {
        "id": "wider_trail_partial_tp",
        "title": "Wider trail / partial TP",
        "gate": ">=30 closes AND expectancy > 0, plus evidence HWM exits leave upside.",
    },
    {
        "id": "maker_signed_deadman",
        "title": "Maker-first + signed AddOrder/Cancel + dead-man",
        "gate": "Required before live arm. Signed private path must exist.",
    },
    {
        "id": "cvd_lead_lag",
        "title": "CVD / lead-lag",
        "gate": "Baseline expectancy proven (>=30 closes and expectancy > 0).",
    },
    {
        "id": "atr_brackets_daily_dd",
        "title": "ATR brackets + hard daily DD gate",
        "gate": "After >=30 closes and positive expectancy; daily DD can be earlier if user asks.",
    },
    {
        "id": "external_watchdog_sms",
        "title": "External watchdog / SMS routing",
        "gate": "Required before live real money. Heartbeat file is scaffolding only.",
    },
    {
        "id": "live_safety_alerts_heartbeat",
        "title": "LIVE safety alerts + heartbeat file",
        "gate": "Auth/connectivity/stale alerts + data/bot_heartbeat.json (token-free).",
    },
    {
        "id": "dead_man_switch",
        "title": "Kraken CancelAllOrdersAfter dead-man",
        "gate": "Arm only when truly live and private Kraken signing is available.",
    },
]

_ITEM_IDS = {spec["id"] for spec in ITEM_SPECS}


def default_future_pack_path(settings: Any = None) -> Path:
    if settings is not None:
        custom = getattr(settings, "future_pack_path", None)
        if custom:
            return Path(str(custom))
        paper = getattr(settings, "paper_book_path", None)
        if paper:
            return Path(str(paper)).expanduser().resolve().parent / "future_pack.json"
    return Path(__file__).resolve().parents[2] / "data" / "future_pack.json"


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _iso(),
        "greenlights": {},
        "manual": {},
        "items": {},
        "n_closes": 0,
        "expectancy": 0.0,
        "formula_score": None,
    }


def load_state(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    dest = Path(path) if path else default_future_pack_path()
    if dest.exists():
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("greenlights", {})
                data.setdefault("manual", {})
                data.setdefault("items", {})
                data.setdefault("version", 1)
                return data
        except Exception:  # noqa: BLE001
            pass
    return empty_state()


def save_state(state: Dict[str, Any], path: Optional[Union[str, Path]] = None) -> Path:
    dest = Path(path) if path else default_future_pack_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated_at"] = _iso()
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(dest)
    return dest


def mark_done(
    item_id: str,
    *,
    path: Optional[Union[str, Path]] = None,
    reason: str = "",
    internal: bool = True,
) -> Dict[str, Any]:
    """Manually/internally mark an item DONE in runtime state."""
    if item_id not in _ITEM_IDS:
        raise KeyError(f"unknown future pack item: {item_id}")
    dest = Path(path) if path else default_future_pack_path()
    state = load_state(dest)
    state.setdefault("manual", {})[item_id] = STATUS_DONE
    notes = state.setdefault("manual_notes", {})
    notes[item_id] = reason or ("internally marked DONE" if internal else "manually marked DONE")
    save_state(state, dest)
    return state


def apply_greenlight(
    item_id: str,
    token: Any,
    *,
    path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Record an explicit user greenlight. The token \"v\" is ignored."""
    if item_id not in _ITEM_IDS:
        raise KeyError(f"unknown future pack item: {item_id}")
    dest = Path(path) if path else default_future_pack_path()
    state = load_state(dest)
    if str(token).strip().lower() == "v":
        # Explicitly not a trigger — persist nothing
        return state
    truthy = str(token).strip().lower() in {"1", "true", "yes", "greenlight", "go", "on"}
    if truthy or token is True:
        state.setdefault("greenlights", {})[item_id] = True
        save_state(state, dest)
    return state


def latest_formula_score(memory_path: Optional[Union[str, Path]] = None) -> Optional[int]:
    path = Path(memory_path) if memory_path else default_formula_memory_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hist = list((data or {}).get("history") or [])
        if not hist:
            return None
        return int(hist[-1].get("score"))
    except Exception:  # noqa: BLE001
        return None


def _trade_closed_at(row: Dict[str, Any]) -> Optional[float]:
    raw = row.get("closed_at") or row.get("when") or row.get("ts")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:  # noqa: BLE001
        return None


def clean_paper_week(
    closed_trades: Sequence[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    min_days: int = 5,
    window_days: int = 7,
) -> bool:
    """True if last 7d has enough distinct days, expectancy > 0, no blow-up day."""
    now_ts = time.time() if now is None else float(now)
    cutoff = now_ts - window_days * 86400.0
    rows: List[Dict[str, Any]] = []
    days = set()
    worst_day: Dict[str, float] = {}
    for t in closed_trades or []:
        ts = _trade_closed_at(t) or now_ts
        if ts < cutoff:
            continue
        rows.append(t)
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        days.add(day)
        try:
            pnl = float(t.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        worst_day[day] = worst_day.get(day, 0.0) + pnl
    if len(days) < int(min_days) or len(rows) < 8:
        return False
    exp = float(_expectancy_from_closes(rows).get("expectancy") or 0.0)
    if exp <= 0:
        return False
    if any(v <= -80.0 for v in worst_day.values()):
        return False
    return True


def hwm_exits_left_upside(closed_trades: Sequence[Dict[str, Any]]) -> bool:
    """Evidence that HWM/trail exits left meaningful upside on the table."""
    leftover = 0
    counted = 0
    for t in closed_trades or []:
        peak = t.get("peak_upl_pct")
        if peak is None:
            continue
        try:
            peak_f = float(peak)
        except (TypeError, ValueError):
            continue
        realized_pct: Optional[float] = None
        try:
            entry = float(t.get("entry") or 0.0)
            exit_px = float(t.get("exit") or t.get("price") or 0.0)
            if entry > 0 and exit_px > 0:
                realized_pct = (exit_px - entry) / entry * 100.0
        except (TypeError, ValueError):
            realized_pct = None
        if realized_pct is None:
            try:
                pnl = float(t.get("pnl") or 0.0)
                qty = float(t.get("qty") or 0.0)
                entry = float(t.get("entry") or 0.0)
                notional = qty * entry
                if notional > 0:
                    realized_pct = pnl / notional * 100.0
            except (TypeError, ValueError):
                continue
        if realized_pct is None:
            continue
        counted += 1
        if peak_f > realized_pct + 0.25:
            leftover += 1
    if counted < 3:
        return False
    return leftover >= max(1, int(0.3 * counted))


def detect_capabilities(broker: Any = None) -> Dict[str, bool]:
    """Detect shipped capabilities. Does not pretend AddOrder/signing exist."""
    live_safety_ok = False
    heartbeat_ok = False
    dead_man_iface = False
    try:
        from trading_bot.utils import live_safety as _ls

        live_safety_ok = hasattr(_ls, "LiveSafetyTracker") and hasattr(
            _ls, "write_bot_heartbeat"
        )
        heartbeat_ok = hasattr(_ls, "write_bot_heartbeat")
    except Exception:  # noqa: BLE001
        live_safety_ok = False
    signing = False
    path_complete = False
    if broker is not None:
        try:
            signing = bool(broker.private_signing_available())
        except Exception:  # noqa: BLE001
            signing = False
        try:
            path_complete = bool(broker.private_live_path_complete())
        except Exception:  # noqa: BLE001
            path_complete = False
        dead_man_iface = callable(getattr(broker, "cancel_all_orders_after", None))
    else:
        try:
            from trading_bot.brokers.kraken import KrakenBroker

            dead_man_iface = callable(getattr(KrakenBroker, "cancel_all_orders_after", None))
            signing = False
            path_complete = False
        except Exception:  # noqa: BLE001
            dead_man_iface = False
    return {
        "live_safety": bool(live_safety_ok),
        "heartbeat_file": bool(heartbeat_ok),
        "dead_man_interface": bool(dead_man_iface),
        "signed_addorder": False,  # live AddOrder still NotImplemented
        "dead_man_armed": bool(signing and path_complete),
        "external_watchdog": False,
        "regime_scaled_size": False,
        "wider_trail_partial_tp": False,
        "cvd_lead_lag": False,
        "atr_brackets": False,
    }


def _baseline_expectancy_proven(n_closes: int, expectancy: float) -> bool:
    return int(n_closes) >= 30 and float(expectancy) > 0.0


def evaluate_item(
    spec: Dict[str, str],
    *,
    n_closes: int,
    expectancy: float,
    capabilities: Dict[str, bool],
    greenlights: Dict[str, Any],
    manual: Dict[str, Any],
    paper_week_clean: bool,
    hwm_upside: bool,
    manual_notes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item_id = spec["id"]
    why = spec["gate"]
    status = STATUS_WAIT
    notes = manual_notes or {}

    if str(manual.get(item_id) or "").upper() == STATUS_DONE:
        return {
            "id": item_id,
            "title": spec["title"],
            "gate": spec["gate"],
            "status": STATUS_DONE,
            "why": str(notes.get(item_id) or "manually/internally marked DONE"),
        }

    if item_id == "live_safety_alerts_heartbeat":
        if capabilities.get("live_safety") and capabilities.get("heartbeat_file"):
            status = STATUS_DONE
            why = "alerts + data/bot_heartbeat.json shipped this revision"
        else:
            why = "live safety module or heartbeat writer missing"
        return {**spec, "status": status, "why": why}

    if item_id == "dead_man_switch":
        if capabilities.get("dead_man_armed"):
            status = STATUS_DONE
            why = "CancelAllOrdersAfter heartbeat armed"
        else:
            status = STATUS_WAIT
            why = "not armed — private live path incomplete"
        return {**spec, "status": status, "why": why}

    if item_id == "regime_scaled_size":
        if capabilities.get("regime_scaled_size"):
            status = STATUS_DONE
            why = "regime-scaled size present in code"
        elif greenlights.get(item_id) or paper_week_clean:
            status = STATUS_READY
            why = (
                "user greenlight recorded"
                if greenlights.get(item_id)
                else "clean paper week satisfied"
            )
        else:
            why = f"{n_closes} closes; no explicit greenlight; no clean paper week (\"v\" ignored)"
        return {**spec, "status": status, "why": why}

    if item_id == "wider_trail_partial_tp":
        if capabilities.get("wider_trail_partial_tp"):
            status = STATUS_DONE
            why = "wider trail / partial TP present in code"
        elif _baseline_expectancy_proven(n_closes, expectancy) and hwm_upside:
            status = STATUS_READY
            why = f"{n_closes} closes, expectancy ${expectancy:.2f}, HWM leftover upside"
        else:
            bits = []
            if n_closes < 30:
                bits.append(f"{n_closes}/30 closes")
            if expectancy <= 0:
                bits.append(f"expectancy ${expectancy:.2f} (need >0)")
            if not hwm_upside:
                bits.append("no HWM leftover-upside evidence")
            why = "; ".join(bits) or spec["gate"]
        return {**spec, "status": status, "why": why}

    if item_id == "maker_signed_deadman":
        if capabilities.get("signed_addorder") and capabilities.get("dead_man_armed"):
            status = STATUS_DONE
            why = "signed AddOrder/Cancel + dead-man present"
        else:
            status = STATUS_WAIT
            why = "blocked: live signed AddOrder is NotImplemented; dead-man not armed"
        return {**spec, "status": status, "why": why}

    if item_id == "cvd_lead_lag":
        if capabilities.get("cvd_lead_lag"):
            status = STATUS_DONE
            why = "CVD / lead-lag present in code"
        elif _baseline_expectancy_proven(n_closes, expectancy):
            status = STATUS_READY
            why = f"baseline expectancy proven ({n_closes} closes, ${expectancy:.2f}/trade)"
        else:
            why = f"{n_closes}/30 closes, expectancy ${expectancy:.2f} (need >0)"
        return {**spec, "status": status, "why": why}

    if item_id == "atr_brackets_daily_dd":
        if capabilities.get("atr_brackets"):
            status = STATUS_DONE
            why = "ATR brackets + hard daily DD present"
        elif _baseline_expectancy_proven(n_closes, expectancy):
            status = STATUS_READY
            why = f"{n_closes} closes and positive expectancy — pack may be considered"
        else:
            why = f"{n_closes}/30 closes, expectancy ${expectancy:.2f}; daily DD may be earlier if you ask"
        return {**spec, "status": status, "why": why}

    if item_id == "external_watchdog_sms":
        if capabilities.get("external_watchdog"):
            status = STATUS_DONE
            why = "external watchdog / SMS routing present"
        else:
            status = STATUS_WAIT
            why = "heartbeat file is scaffolding only — external supervisor/SMS not built"
        return {**spec, "status": status, "why": why}

    return {**spec, "status": STATUS_WAIT, "why": why}


def mark_detected_capabilities(
    *,
    path: Optional[Union[str, Path]] = None,
    broker: Any = None,
    capabilities: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Persist DONE for capabilities this revision actually shipped."""
    dest = Path(path) if path else default_future_pack_path()
    state = load_state(dest)
    caps = capabilities if capabilities is not None else detect_capabilities(broker)
    if caps.get("live_safety") and caps.get("heartbeat_file"):
        state.setdefault("manual", {})["live_safety_alerts_heartbeat"] = STATUS_DONE
        state.setdefault("manual_notes", {})["live_safety_alerts_heartbeat"] = (
            "shipped: LIVE alerts + data/bot_heartbeat.json"
        )
    # dead-man stays WAIT unless truly armed
    if not caps.get("dead_man_armed"):
        if state.get("manual", {}).get("dead_man_switch") == STATUS_DONE:
            state["manual"].pop("dead_man_switch", None)
    save_state(state, dest)
    return state


def evaluate_future_pack(
    *,
    closed_trades: Optional[Sequence[Dict[str, Any]]] = None,
    formula_score: Optional[int] = None,
    expectancy: Optional[float] = None,
    n_closes: Optional[int] = None,
    path: Optional[Union[str, Path]] = None,
    broker: Any = None,
    settings: Any = None,
    persist: bool = True,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Build WAIT/READY/DONE snapshot. Never mutates trading knobs."""
    dest = Path(path) if path else default_future_pack_path(settings)
    state = load_state(dest)
    trades = list(closed_trades or [])
    exp_info = _expectancy_from_closes(trades)
    n = int(n_closes if n_closes is not None else exp_info.get("n") or len(trades))
    exp = float(expectancy if expectancy is not None else exp_info.get("expectancy") or 0.0)
    score = formula_score
    if score is None:
        score = latest_formula_score()
    try:
        score_i = int(score) if score is not None else None
    except (TypeError, ValueError):
        score_i = None

    caps = detect_capabilities(broker)
    # Ensure shipped live-safety is recorded DONE in runtime state
    mark_detected_capabilities(path=dest, broker=broker, capabilities=caps)
    state = load_state(dest)

    paper_week = clean_paper_week(trades, now=now)
    hwm = hwm_exits_left_upside(trades)
    items: List[Dict[str, Any]] = []
    for spec in ITEM_SPECS:
        row = evaluate_item(
            spec,
            n_closes=n,
            expectancy=exp,
            capabilities=caps,
            greenlights=state.get("greenlights") or {},
            manual=state.get("manual") or {},
            manual_notes=state.get("manual_notes") or {},
            paper_week_clean=paper_week,
            hwm_upside=hwm,
        )
        items.append(row)

    wait_n = sum(1 for i in items if i["status"] == STATUS_WAIT)
    ready_n = sum(1 for i in items if i["status"] == STATUS_READY)
    done_n = sum(1 for i in items if i["status"] == STATUS_DONE)
    urgent = bool(score_i is not None and score_i >= URGENT_SCORE and wait_n > 0)

    pack = {
        "version": 1,
        "updated_at": _iso(),
        "formula_score": score_i,
        "formula_band": band_for_score(score_i) if score_i is not None else None,
        "n_closes": n,
        "expectancy": exp,
        "clean_paper_week": paper_week,
        "hwm_left_upside": hwm,
        "items": items,
        "counts": {"WAIT": wait_n, "READY": ready_n, "DONE": done_n},
        "urgent": urgent,
        "auto_apply": False,
        "capabilities": caps,
        "greenlights": dict(state.get("greenlights") or {}),
        "path": str(dest),
    }
    if persist:
        persist_body = {
            "version": 1,
            "updated_at": pack["updated_at"],
            "formula_score": score_i,
            "n_closes": n,
            "expectancy": exp,
            "greenlights": pack["greenlights"],
            "manual": dict(state.get("manual") or {}),
            "manual_notes": dict(state.get("manual_notes") or {}),
            "items": {i["id"]: {"status": i["status"], "why": i["why"]} for i in items},
            "urgent": urgent,
            "auto_apply": False,
        }
        save_state(persist_body, dest)
    return pack


def format_future_pack_report(pack: Dict[str, Any]) -> str:
    score = pack.get("formula_score")
    band = pack.get("formula_band") or (band_for_score(int(score)) if score is not None else "?")
    icon = band_icon(str(band)) if score is not None else "⚪"
    score_s = f"{icon} {int(score)}" if score is not None else "n/a"
    exp = float(pack.get("expectancy") or 0.0)
    n = int(pack.get("n_closes") or 0)
    lines = [
        "📦 Future Pack readiness",
        f"formula: {score_s}  ·  closes={n}  ·  expectancy=${exp:.2f}/trade",
        "(score in the 70s is a deadline/goal — not permission to auto-implement)",
        "",
    ]
    if pack.get("urgent"):
        wait_n = int((pack.get("counts") or {}).get("WAIT") or 0)
        lines.append("🚨 URGENT — Before health score is in the 70s")
        lines.append(
            f"Formula score is {int(score) if score is not None else '?'} while "
            f"{wait_n} pack item(s) remain WAIT."
        )
        lines.append(
            "Score is a readiness goal, not a green light to ship risky features."
        )
        lines.append("")
    for item in pack.get("items") or []:
        status = str(item.get("status") or STATUS_WAIT)
        badge = {"WAIT": "WAIT", "READY": "READY", "DONE": "DONE"}.get(status, status)
        lines.append(f"• {badge}  {item.get('title')}")
        why = (item.get("why") or item.get("gate") or "").strip()
        if why:
            lines.append(f"    {why}")
    lines.append("")
    lines.append("Blocked prerequisite: live signed AddOrder (NotImplemented).")
    lines.append("Do not auto-edit trading knobs. \"v\" is not a greenlight.")
    return "\n".join(lines)
