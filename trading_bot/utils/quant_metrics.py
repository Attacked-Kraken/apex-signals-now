"""Institutional-style session risk metrics (pure helpers, no network).

Memory + visibility only — never mutates settings / formula knobs.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

Number = Union[int, float]
ReturnsLike = Sequence[Number]

_PNL_KEYS = ("pnl", "realized_pnl", "pl", "net_pnl")


def _safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def max_drawdown_pct(equity_peak: float, equity_now: float) -> float:
    """Peak-to-now drawdown as a positive percentage.

    Returns 0.0 when peak is non-positive or equity is at/above peak.
    """
    peak = _safe_float(equity_peak, 0.0) or 0.0
    now = _safe_float(equity_now, 0.0) or 0.0
    if peak <= 0.0:
        return 0.0
    dd = (peak - now) / peak * 100.0
    return max(0.0, float(dd))


def extract_trade_pnl(trade: Any) -> Optional[float]:
    """Pull pnl from a closed-trade dict; try common field names."""
    if not isinstance(trade, dict):
        return None
    for key in _PNL_KEYS:
        if key in trade and trade[key] is not None:
            val = _safe_float(trade[key])
            if val is not None:
                return val
    return None


def returns_from_closed_trades(
    trades: Optional[Sequence[Dict[str, Any]]],
) -> List[float]:
    """Extract per-trade pnl (or pct) series; skip malformed rows."""
    out: List[float] = []
    for t in trades or []:
        pnl = extract_trade_pnl(t)
        if pnl is None:
            continue
        out.append(float(pnl))
    return out


def sharpe_like(
    returns: Optional[Iterable[Number]],
    periods_per_year: Optional[float] = None,
) -> Optional[float]:
    """Simple sample Sharpe of per-trade returns. None if fewer than 5 samples."""
    xs = [float(x) for x in (returns or []) if x is not None]
    n = len(xs)
    if n < 5:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 1e-12:
        return None
    ratio = mean / std
    if periods_per_year is not None:
        try:
            ppy = float(periods_per_year)
            if ppy > 0:
                ratio *= math.sqrt(ppy)
        except (TypeError, ValueError):
            pass
    return float(ratio)


def sortino_like(returns: Optional[Iterable[Number]]) -> Optional[float]:
    """Mean / downside deviation (negative returns only). None if <5 samples."""
    xs = [float(x) for x in (returns or []) if x is not None]
    n = len(xs)
    if n < 5:
        return None
    mean = sum(xs) / n
    downs = [x for x in xs if x < 0.0]
    if not downs:
        # No downside — treat as very high Sortino when mean > 0
        return float(mean / 1e-9) if mean > 0 else 0.0
    # Downside deviation vs 0 target (population over full sample length)
    dd_var = sum(min(x, 0.0) ** 2 for x in xs) / n
    dd = math.sqrt(dd_var) if dd_var > 0 else 0.0
    if dd <= 1e-12:
        return None
    return float(mean / dd)


def expectancy(
    avg_win: float,
    avg_loss: float,
    win_rate: float,
) -> float:
    """E = wr * avg_win + (1-wr) * avg_loss.

    ``avg_loss`` may be negative (signed) or positive magnitude; if positive
    and wins are expected positive, treat as loss magnitude (subtract).
    """
    try:
        wr = max(0.0, min(1.0, float(win_rate)))
        aw = float(avg_win)
        al = float(avg_loss)
    except (TypeError, ValueError):
        return 0.0
    # If avg_loss given as positive magnitude, convert to signed
    if al > 0 and aw >= 0:
        al = -abs(al)
    return wr * aw + (1.0 - wr) * al


def expectancy_from_trades(
    trades: Optional[Sequence[Dict[str, Any]]],
) -> Dict[str, float]:
    """Avg win / avg loss / expectancy / n / win_rate from closed trades."""
    wins: List[float] = []
    losses: List[float] = []
    for t in trades or []:
        pnl = extract_trade_pnl(t)
        if pnl is None:
            continue
        won = None
        if isinstance(t, dict):
            won = t.get("won")
        if won is None:
            won = pnl > 0
        if won:
            wins.append(pnl)
        else:
            losses.append(pnl)
    n = len(wins) + len(losses)
    if n == 0:
        return {
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "win_rate": 0.0,
            "n": 0.0,
        }
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    wr = len(wins) / n
    exp = expectancy(avg_win, avg_loss, wr)
    return {
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "expectancy": float(exp),
        "win_rate": float(wr),
        "n": float(n),
    }


def format_quant_status_line(
    *,
    dd_pct: Optional[float] = None,
    sharpe: Optional[float] = None,
    sortino: Optional[float] = None,
    n_trades: Optional[int] = None,
) -> str:
    """Compact status fragment, e.g. ``quant: DD 3.2% · Sortino 1.1 · n=12``."""
    parts: List[str] = []
    if dd_pct is not None:
        try:
            parts.append(f"DD {float(dd_pct):.1f}%")
        except (TypeError, ValueError):
            pass
    if sortino is not None:
        try:
            parts.append(f"Sortino {float(sortino):.1f}")
        except (TypeError, ValueError):
            pass
    if sharpe is not None:
        try:
            parts.append(f"Sharpe {float(sharpe):.1f}")
        except (TypeError, ValueError):
            pass
    if n_trades is not None:
        try:
            parts.append(f"n={int(n_trades)}")
        except (TypeError, ValueError):
            pass
    if not parts:
        return "quant: n/a"
    return "quant: " + " · ".join(parts)


def dd_failure_gate(*, dd_pct: float, limit_pct: float) -> Optional[str]:
    """Return reason if drawdown breaches limit; else None."""
    try:
        dd = float(dd_pct)
        lim = float(limit_pct)
    except (TypeError, ValueError):
        return None
    if dd >= lim:
        return f"max DD {dd:.1f}% ≥ {lim:.0f}%"
    return None


def update_equity_peak(prev_peak: Any, equity: Any) -> float:
    """Return max(prev_peak, equity), seeding from equity when peak missing/zero."""
    eq = _safe_float(equity, 0.0) or 0.0
    prev = _safe_float(prev_peak, 0.0) or 0.0
    if prev <= 0.0:
        return max(0.0, eq)
    return max(prev, eq)


def compute_quant_snapshot(
    *,
    equity: float,
    equity_peak: float,
    closed_trades: Optional[Sequence[Dict[str, Any]]] = None,
    phd_mode: bool = False,
    phd_max_dd_pct: float = 8.0,
    formula_band: Optional[str] = None,
) -> Dict[str, Any]:
    """Bundle DD / Sharpe / Sortino / expectancy for status + /quant."""
    peak = float(equity_peak or 0.0)
    eq = float(equity or 0.0)
    dd = max_drawdown_pct(peak, eq)
    rets = returns_from_closed_trades(closed_trades)
    sharpe = sharpe_like(rets)
    sortino = sortino_like(rets)
    exp_info = expectancy_from_trades(closed_trades)
    n = int(exp_info.get("n") or len(rets))
    limit = float(phd_max_dd_pct)
    gate_reason = dd_failure_gate(dd_pct=dd, limit_pct=limit)
    gate_would_fire = bool(phd_mode and gate_reason)
    return {
        "dd_pct": dd,
        "equity": eq,
        "equity_peak": peak,
        "sharpe": sharpe,
        "sortino": sortino,
        "expectancy": float(exp_info.get("expectancy") or 0.0),
        "avg_win": float(exp_info.get("avg_win") or 0.0),
        "avg_loss": float(exp_info.get("avg_loss") or 0.0),
        "win_rate": float(exp_info.get("win_rate") or 0.0),
        "n_trades": n,
        "phd_mode": bool(phd_mode),
        "phd_max_dd_pct": limit,
        "dd_gate_reason": gate_reason,
        "dd_gate_would_fire": gate_would_fire,
        "formula_band": str(formula_band or "") or None,
        "status_line": format_quant_status_line(
            dd_pct=dd,
            sharpe=sharpe,
            sortino=sortino,
            n_trades=n if n > 0 or dd > 0 else n,
        ),
    }


def format_quant_report(snap: Dict[str, Any]) -> str:
    """Full short /quant report."""
    lines: List[str] = [
        "📊 Quant — session risk health (not alpha proof)",
        "",
        "Metrics:",
    ]
    dd = snap.get("dd_pct")
    peak = snap.get("equity_peak")
    eq = snap.get("equity")
    try:
        lines.append(f"• DD {float(dd):.1f}%  (peak ${float(peak):.2f} → equity ${float(eq):.2f})")
    except (TypeError, ValueError):
        lines.append("• DD n/a")

    sharpe = snap.get("sharpe")
    sortino = snap.get("sortino")
    if sharpe is not None:
        lines.append(f"• Sharpe {float(sharpe):.2f}")
    else:
        lines.append("• Sharpe n/a (<5 trades)")
    if sortino is not None:
        lines.append(f"• Sortino {float(sortino):.2f}")
    else:
        lines.append("• Sortino n/a (<5 trades)")

    try:
        exp = float(snap.get("expectancy") or 0.0)
        n = int(snap.get("n_trades") or 0)
        wr = float(snap.get("win_rate") or 0.0) * 100.0
        lines.append(f"• Expectancy ${exp:.2f}/trade  ·  WR {wr:.0f}%  ·  n={n}")
    except (TypeError, ValueError):
        lines.append("• Expectancy n/a")

    lines.append("")
    lines.append("Failure modes:")
    limit = float(snap.get("phd_max_dd_pct") or 8.0)
    phd_on = bool(snap.get("phd_mode"))
    if snap.get("dd_gate_would_fire"):
        lines.append(
            f"• DD gate: FIRE — max DD {float(dd):.1f}% ≥ {limit:.0f}% "
            f"(phd ON; new entries paused)"
        )
    elif phd_on:
        lines.append(
            f"• DD gate: ok — DD {float(dd):.1f}% < {limit:.0f}% (phd ON)"
        )
    else:
        lines.append(
            f"• DD gate: idle — limit {limit:.0f}% (phd OFF; gate inactive)"
        )

    band = str(snap.get("formula_band") or "").strip().upper()
    if phd_on:
        if band == "WEAK":
            lines.append("• Formula WEAK soft gate: would block new entries (phd ON)")
        elif band:
            lines.append(f"• Formula WEAK soft gate: ok (band {band})")
        else:
            lines.append("• Formula WEAK soft gate: available when phd ON")
    else:
        lines.append("• Formula WEAK soft gate: idle (phd OFF)")

    return "\n".join(lines)
