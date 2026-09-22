"""Heuristic formula health score (0–100, higher = healthier).

Opposite of api_risk: STRONG ≥70, OK 45–69, WEAK <45.
Memory + suggest only — never mutates settings / .env.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HISTORY_MAX = 50


def default_formula_memory_path(settings: Any = None) -> Path:
    """Resolve data/formula_memory.json under the bot root."""
    if settings is not None:
        custom = getattr(settings, "formula_memory_path", None)
        if custom:
            return Path(str(custom))
        paper = getattr(settings, "paper_book_path", None)
        if paper:
            return Path(str(paper)).expanduser().resolve().parent / "formula_memory.json"
    # trading_bot/utils/formula_score.py → parents[2] = bot root
    return Path(__file__).resolve().parents[2] / "data" / "formula_memory.json"


def band_for_score(score: int) -> str:
    s = int(score)
    if s >= 70:
        return "STRONG"
    if s >= 45:
        return "OK"
    return "WEAK"


def band_icon(band: str) -> str:
    return {"STRONG": "🟢", "OK": "🟡", "WEAK": "🔴"}.get(str(band).upper(), "⚪")


@dataclass
class FormulaAdjustment:
    priority: int
    reason: str
    tip: str
    action: str  # pasteable Telegram command or .env key hint

    def as_dict(self) -> Dict[str, Any]:
        return {
            "priority": int(self.priority),
            "reason": self.reason,
            "tip": self.tip,
            "action": self.action,
        }


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None:
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _expectancy_from_closes(closed: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Avg win / avg loss / expectancy from paper closed_trades."""
    wins_pnl: List[float] = []
    losses_pnl: List[float] = []
    for t in closed or []:
        try:
            pnl = float(t.get("pnl") if t.get("pnl") is not None else 0.0)
        except (TypeError, ValueError):
            continue
        won = t.get("won")
        if won is None:
            won = pnl > 0
        if won:
            wins_pnl.append(pnl)
        else:
            losses_pnl.append(pnl)
    avg_win = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0.0
    avg_loss = sum(losses_pnl) / len(losses_pnl) if losses_pnl else 0.0  # typically ≤0
    n = len(wins_pnl) + len(losses_pnl)
    if n == 0:
        return {"avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0, "n": 0}
    wr = len(wins_pnl) / n
    exp = wr * avg_win + (1.0 - wr) * avg_loss
    return {"avg_win": avg_win, "avg_loss": avg_loss, "expectancy": exp, "n": n}


def evaluate_formula(
    *,
    wins: int = 0,
    losses: int = 0,
    win_rate_pct: Optional[float] = None,
    consecutive_losses: int = 0,
    entry_threshold: float = 60.0,
    winning_formula: bool = False,
    max_spread_pct: float = 0.0025,
    trade_profile: str = "medium",
    stop_loss_profile: str = "medium",
    focus_block_reason: Optional[str] = None,
    focus_blocked: bool = False,
    paper_equity: Optional[float] = None,
    starting_equity: Optional[float] = None,
    wallet_b4: Optional[float] = None,
    positions_count: int = 0,
    max_concurrent_positions: int = 3,
    exposure_usd: Optional[float] = None,
    max_total_exposure_usd: Optional[float] = None,
    paused: bool = False,
    circuit_breaker_on: bool = False,
    circuit_breaker_tripped: bool = False,
    closed_trades: Optional[Sequence[Dict[str, Any]]] = None,
    cash: Optional[float] = None,
) -> Dict[str, Any]:
    """Return score 0–100 (higher = healthier formula), band, components, adjustments.

    Suggest-only: never mutates caller state.
    """
    w = max(0, _safe_int(wins))
    l = max(0, _safe_int(losses))
    n_trades = w + l
    if win_rate_pct is not None:
        wr = max(0.0, min(100.0, _safe_float(win_rate_pct)))
    elif n_trades > 0:
        wr = 100.0 * w / n_trades
    else:
        wr = None

    consec = max(0, _safe_int(consecutive_losses))
    thresh = _safe_float(entry_threshold, 60.0)
    spread = _safe_float(max_spread_pct, 0.0025)
    block = (focus_block_reason or "").strip()
    blocked = bool(focus_blocked) or bool(block)
    block_l = block.lower()

    # --- baseline ---
    score = 55.0
    components: List[Dict[str, Any]] = [
        {"name": "baseline", "delta": 55, "note": "neutral start (few/no trades → ~55)"},
    ]
    adjustments: List[FormulaAdjustment] = []

    def add(delta: float, name: str, note: str) -> None:
        nonlocal score
        score += delta
        components.append({"name": name, "delta": round(delta, 1), "note": note})

    # Win rate / sample
    if n_trades == 0:
        add(0, "sample", "no closed trades yet — holding baseline")
    elif n_trades < 5:
        add(-2, "sample", f"small sample ({n_trades} trades) — score cautious")
        if wr is not None and wr >= 55:
            add(6, "win_rate", f"WR {wr:.0f}% early positive")
        elif wr is not None and wr < 40:
            add(-10, "win_rate", f"WR {wr:.0f}% early weak")
    else:
        if wr is not None:
            if wr >= 65:
                add(18, "win_rate", f"WR {wr:.0f}% strong ({w}W/{l}L)")
            elif wr >= 55:
                add(10, "win_rate", f"WR {wr:.0f}% solid ({w}W/{l}L)")
            elif wr >= 45:
                add(2, "win_rate", f"WR {wr:.0f}% middling ({w}W/{l}L)")
            elif wr >= 35:
                add(-12, "win_rate", f"WR {wr:.0f}% soft ({w}W/{l}L)")
            else:
                add(-22, "win_rate", f"WR {wr:.0f}% poor ({w}W/{l}L)")

    # Expectancy from closed trades
    exp_info = _expectancy_from_closes(closed_trades or [])
    if exp_info["n"] >= 3:
        exp = exp_info["expectancy"]
        if exp > 1.0:
            add(8, "expectancy", f"expectancy +${exp:.2f}/trade")
        elif exp > 0:
            add(3, "expectancy", f"expectancy +${exp:.2f}/trade")
        elif exp > -2.0:
            add(-6, "expectancy", f"expectancy ${exp:.2f}/trade")
        else:
            add(-14, "expectancy", f"expectancy ${exp:.2f}/trade (leaking)")

    # Consecutive losses / CB proximity
    if consec >= 3:
        add(-18, "consec_losses", f"{consec} consecutive losses (near/at CB)")
    elif consec == 2:
        add(-10, "consec_losses", "2 consecutive losses")
    elif consec == 1:
        add(-4, "consec_losses", "1 consecutive loss")
    else:
        add(3, "consec_losses", "no consecutive loss streak")

    if circuit_breaker_tripped or (paused and consec >= 3):
        add(-8, "circuit_breaker", "circuit breaker tripped / paused after losses")
    elif circuit_breaker_on:
        add(2, "circuit_breaker", "CB armed (auto-pause on streak) ✓")

    if paused and not (circuit_breaker_tripped or consec >= 3):
        add(-3, "paused", "manually paused — no new entries")

    # Focus blocks
    if blocked:
        if "cash" in block_l or "insufficient" in block_l:
            add(-10, "focus_block", f"blocked: {block[:80]}")
        elif "exposure" in block_l or "cap" in block_l or "concurrent" in block_l:
            add(-8, "focus_block", f"blocked: {block[:80]}")
        elif "api_risk" in block_l:
            add(-6, "focus_block", f"blocked: {block[:80]}")
        else:
            add(-5, "focus_block", f"blocked: {block[:80] or 'entries frozen'}")

    # Equity vs starting / wallet
    start = None
    for cand in (starting_equity, wallet_b4):
        if cand is not None and _safe_float(cand) > 0:
            start = _safe_float(cand)
            break
    eq = _safe_float(paper_equity) if paper_equity is not None else None
    if start and eq is not None and start > 0:
        ret_pct = 100.0 * (eq - start) / start
        if ret_pct >= 3:
            add(10, "equity", f"equity +{ret_pct:.1f}% vs start")
        elif ret_pct >= 0:
            add(3, "equity", f"equity +{ret_pct:.1f}% vs start")
        elif ret_pct >= -3:
            add(-4, "equity", f"equity {ret_pct:.1f}% vs start")
        else:
            add(-12, "equity", f"equity {ret_pct:.1f}% vs start")

    # Exposure / position pressure
    max_pos = max(1, _safe_int(max_concurrent_positions, 3))
    pos_n = max(0, _safe_int(positions_count))
    if pos_n >= max_pos:
        add(-6, "positions", f"at max positions ({pos_n}/{max_pos})")
    elif pos_n == max_pos - 1 and max_pos > 1:
        add(-2, "positions", f"near max positions ({pos_n}/{max_pos})")

    max_exp = _safe_float(max_total_exposure_usd) if max_total_exposure_usd is not None else None
    exp_usd = _safe_float(exposure_usd) if exposure_usd is not None else None
    if max_exp and max_exp > 0 and exp_usd is not None:
        frac = exp_usd / max_exp
        if frac >= 0.95:
            add(-8, "exposure", f"exposure {frac*100:.0f}% of cap")
        elif frac >= 0.8:
            add(-3, "exposure", f"exposure {frac*100:.0f}% of cap")

    # Cash pressure
    if cash is not None and max_exp and max_exp > 0:
        c = _safe_float(cash)
        if c < max_exp * 0.15:
            add(-6, "cash", f"low cash ${c:.0f} vs exposure cap")

    # Threshold / WF / spread / profile knobs
    if winning_formula:
        add(5, "winning_formula", "winning_formula ON ✓")
    else:
        add(-2, "winning_formula", "winning_formula OFF")

    if thresh >= 85 and n_trades == 0:
        add(-8, "threshold", f"threshold {thresh:.0f}% — likely over-filtering (0 trades)")
    elif thresh >= 80 and n_trades < 3:
        add(-5, "threshold", f"threshold {thresh:.0f}% — selective / sparse")
    elif 55 <= thresh <= 70:
        add(3, "threshold", f"threshold {thresh:.0f}% in sweet zone")
    elif thresh < 40:
        add(-6, "threshold", f"threshold {thresh:.0f}% aggressive — more noise")

    spread_pct = spread * 100.0  # display units
    if spread_pct <= 0.08:
        add(-4, "spread", f"spread cap {spread_pct:g}% very tight")
    elif spread_pct >= 1.0:
        add(-3, "spread", f"spread cap {spread_pct:g}% loose")
    else:
        add(1, "spread", f"spread cap {spread_pct:g}%")

    prof = str(trade_profile or "medium").strip().lower()
    if prof == "aggressive" and wr is not None and wr < 45 and n_trades >= 5:
        add(-5, "profile", "aggressive profile + soft WR")
    elif prof == "low" and n_trades == 0:
        add(-2, "profile", "low profile + zero trades")

    slp = str(stop_loss_profile or "medium").strip().lower()
    if slp == "tight" and consec >= 2:
        add(-2, "stop_loss", "tight SL + loss streak — may be stop-hunted")

    score_i = int(max(0, min(100, round(score))))
    band = band_for_score(score_i)

    # --- ranked adjustments (suggest only) ---
    def suggest(priority: int, reason: str, tip: str, action: str) -> None:
        adjustments.append(
            FormulaAdjustment(priority=priority, reason=reason, tip=tip, action=action)
        )

    if wr is not None and wr < 45 and n_trades >= 3:
        new_thr = min(95, int(round(thresh)) + 5)
        suggest(
            1,
            f"Low WR ({wr:.0f}%) with {l} losses",
            f"Raise entry threshold (+5) to filter weaker setups",
            f"/set_threshold {new_thr}",
        )
        if not winning_formula:
            suggest(
                2,
                "Winning formula off while WR soft",
                "Turn winning_formula ON for Tier-1 stack",
                "/winning_formula on",
            )

    if "cash" in block_l or (
        cash is not None and max_exp and _safe_float(cash) < max_exp * 0.15
    ):
        suggest(
            1 if not adjustments else 3,
            "Cash / size pressure blocking entries",
            "Lower size or free cash; check caps",
            "/set_limit <trade> <book>",
        )

    if "exposure" in block_l or (
        max_exp and exp_usd is not None and exp_usd / max_exp >= 0.9
    ):
        suggest(
            2 if not any(a.priority == 2 for a in adjustments) else 4,
            "Exposure / concurrent pressure",
            "Wait for exits or lower max exposure / concurrent",
            "/set_limit <trade> <book>",
        )

    if consec >= 2:
        suggest(
            1 if consec >= 3 else 3,
            f"{consec} consecutive losses near CB",
            "Pause new entries / wait CB cooldown",
            "/pause",
        )

    if thresh >= 80 and n_trades < 3:
        new_thr = max(15, int(round(thresh)) - 5)
        suggest(
            2,
            f"Threshold {thresh:.0f}% with sparse/zero fills",
            "Lower threshold slightly — over-filtering",
            f"/set_threshold {new_thr}",
        )

    if spread_pct <= 0.08:
        suggest(
            4,
            f"Spread cap {spread_pct:g}% very tight",
            "Widen slightly so liquid majors can pass",
            "/set_spread 0.25",
        )
    elif spread_pct >= 1.0:
        suggest(
            5,
            f"Spread cap {spread_pct:g}% loose",
            "Tighten to avoid wide-spread junk entries",
            "/set_spread 0.25",
        )

    if not winning_formula and score_i < 55 and not any(
        a.action.startswith("/winning_formula") for a in adjustments
    ):
        suggest(
            3,
            "Formula health soft without WF",
            "Consider winning_formula ON",
            "/winning_formula on",
        )

    if circuit_breaker_tripped:
        suggest(
            1,
            "Circuit breaker tripped",
            "Wait cooldown then /resume when ready",
            "/resume",
        )

    if eq is not None and start and start > 0 and (eq - start) / start <= -0.05:
        suggest(
            2,
            "Equity drawdown ≥5% vs start",
            "Switch to tighter profile or pause",
            "/profile low",
        )

    # Deduplicate by action, keep best (lowest) priority
    seen_actions: Dict[str, FormulaAdjustment] = {}
    for adj in adjustments:
        prev = seen_actions.get(adj.action)
        if prev is None or adj.priority < prev.priority:
            seen_actions[adj.action] = adj
    ranked = sorted(seen_actions.values(), key=lambda a: (a.priority, a.reason))
    # Re-number 1..N
    for i, adj in enumerate(ranked, 1):
        adj.priority = i

    if not ranked:
        ranked = [
            FormulaAdjustment(
                priority=1,
                reason="No urgent issues detected",
                tip="Keep monitoring /status; tweak only if WR or blocks worsen",
                action="/status",
            )
        ]

    metrics = {
        "wins": w,
        "losses": l,
        "win_rate_pct": wr,
        "consecutive_losses": consec,
        "entry_threshold": thresh,
        "winning_formula": bool(winning_formula),
        "max_spread_pct": spread,
        "trade_profile": prof,
        "stop_loss_profile": slp,
        "focus_block_reason": block or None,
        "paused": bool(paused),
        "circuit_breaker_on": bool(circuit_breaker_on),
        "circuit_breaker_tripped": bool(circuit_breaker_tripped),
        "positions_count": pos_n,
        "paper_equity": eq,
        "starting_equity": start,
        "expectancy": exp_info.get("expectancy"),
        "closed_trades_n": int(exp_info.get("n") or 0),
        "avg_win": exp_info.get("avg_win"),
        "avg_loss": exp_info.get("avg_loss"),
    }

    return {
        "score": score_i,
        "band": band,
        "components": components,
        "adjustments": [a.as_dict() for a in ranked],
        "metrics": metrics,
        "ts": time.time(),
        "auto_apply": False,
    }


def format_formula_status_line(snap: Dict[str, Any]) -> str:
    """Compact /status line: formula: 🟢 78"""
    score = int(snap.get("score") or 0)
    band = str(snap.get("band") or band_for_score(score))
    icon = band_icon(band)
    return f"formula: {icon} {score}"


def format_formula_report(snap: Dict[str, Any]) -> str:
    """Full /formula report — score, band, components, ranked adjustments."""
    score = int(snap.get("score") or 0)
    band = str(snap.get("band") or band_for_score(score))
    icon = band_icon(band)
    m = snap.get("metrics") or {}
    lines = [
        f"Formula health: {icon} {band} ({score}/100)",
        "(heuristic · suggest-only — never auto-applies knobs)",
        "",
    ]
    wr = m.get("win_rate_pct")
    wr_s = f"{float(wr):.0f}%" if wr is not None else "—"
    lines.append(
        f"WR {wr_s} · {int(m.get('wins') or 0)}W/{int(m.get('losses') or 0)}L · "
        f"consec_losses={int(m.get('consecutive_losses') or 0)}"
    )
    lines.append(
        f"threshold={_safe_float(m.get('entry_threshold')):.0f}% · "
        f"WF={'ON' if m.get('winning_formula') else 'OFF'} · "
        f"spread={_safe_float(m.get('max_spread_pct')) * 100:g}% · "
        f"profile={m.get('trade_profile') or '?'}"
    )
    if m.get("focus_block_reason"):
        lines.append(f"focus_block: {m['focus_block_reason']}")
    if m.get("expectancy") is not None and (m.get("wins", 0) + m.get("losses", 0)) >= 1:
        lines.append(
            f"expectancy≈${_safe_float(m.get('expectancy')):.2f}/trade "
            f"(avgW ${_safe_float(m.get('avg_win')):.2f} / avgL ${_safe_float(m.get('avg_loss')):.2f})"
        )
    lines.append("")
    lines.append("Components:")
    for c in snap.get("components") or []:
        d = c.get("delta", 0)
        sign = "+" if float(d) >= 0 else ""
        lines.append(f"• {c.get('name')}: {sign}{d} — {c.get('note')}")

    adjs = snap.get("adjustments") or []
    if adjs:
        lines.append("")
        lines.append("Ranked adjustments (manual apply):")
        for a in adjs:
            lines.append(
                f"{int(a.get('priority') or 0)}. {a.get('reason')}"
            )
            tip = (a.get("tip") or "").strip()
            if tip:
                lines.append(f"   → {tip}")
            action = (a.get("action") or "").strip()
            if action:
                lines.append(f"   action: {action}")
    lines.append("")
    lines.append("Mode: memory + suggest only (no auto param edits).")
    return "\n".join(lines)


class FormulaMemory:
    """Persist rolling formula snapshots + last_suggestions (no secrets)."""

    def __init__(self, path: Optional[Path] = None, *, settings: Any = None):
        self.path = Path(path) if path else default_formula_memory_path(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("history", [])
                    data.setdefault("last_suggestions", [])
                    return data
            except Exception:  # noqa: BLE001
                pass
        return {"history": [], "last_suggestions": [], "version": 1}

    def save(self, data: Dict[str, Any]) -> None:
        payload = {
            "version": int(data.get("version") or 1),
            "history": list(data.get("history") or [])[-HISTORY_MAX:],
            "last_suggestions": list(data.get("last_suggestions") or []),
            "updated_at": time.time(),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _snapshot_for_memory(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Strip to safe fields — never store secrets."""
    m = snap.get("metrics") or {}
    safe_metrics = {
        "wins": m.get("wins"),
        "losses": m.get("losses"),
        "win_rate_pct": m.get("win_rate_pct"),
        "consecutive_losses": m.get("consecutive_losses"),
        "entry_threshold": m.get("entry_threshold"),
        "winning_formula": m.get("winning_formula"),
        "max_spread_pct": m.get("max_spread_pct"),
        "trade_profile": m.get("trade_profile"),
        "stop_loss_profile": m.get("stop_loss_profile"),
        "focus_block_reason": m.get("focus_block_reason"),
        "paused": m.get("paused"),
        "positions_count": m.get("positions_count"),
        "paper_equity": m.get("paper_equity"),
        "expectancy": m.get("expectancy"),
        "closed_trades_n": m.get("closed_trades_n"),
    }
    top3 = list(snap.get("adjustments") or [])[:3]
    return {
        "ts": snap.get("ts") or time.time(),
        "score": int(snap.get("score") or 0),
        "band": snap.get("band"),
        "metrics": safe_metrics,
        "top_adjustments": top3,
    }


def record_snapshot(memory_path: Path, snap: Dict[str, Any]) -> Dict[str, Any]:
    """Append snapshot to rolling history; update last_suggestions. Returns memory dict."""
    mem = FormulaMemory(path=Path(memory_path))
    data = mem.load()
    entry = _snapshot_for_memory(snap)
    hist = list(data.get("history") or [])
    hist.append(entry)
    data["history"] = hist[-HISTORY_MAX:]
    data["last_suggestions"] = list(snap.get("adjustments") or [])
    mem.save(data)
    return data


@dataclass
class FormulaScoreTracker:
    """Thin stateful wrapper mirroring BanRiskTracker style (optional)."""

    memory_path: Optional[Path] = None
    last_score: int = 55
    last_band: str = "OK"
    last_snap: Dict[str, Any] = field(default_factory=dict)

    def evaluate(self, **kwargs: Any) -> Dict[str, Any]:
        snap = evaluate_formula(**kwargs)
        self.last_score = int(snap["score"])
        self.last_band = str(snap["band"])
        self.last_snap = snap
        return snap

    def record(self, snap: Optional[Dict[str, Any]] = None, *, settings: Any = None) -> Dict[str, Any]:
        path = self.memory_path or default_formula_memory_path(settings)
        return record_snapshot(path, snap or self.last_snap)
