"""Heuristic API abuse / platform ban-risk hygiene score.

Not a true probability — a live scorecard from our own traffic signals
(429s, breaker trips, pacing, paper vs live, official endpoints).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class BanRiskTracker:
    """Rolling window tracker for Kraken / Telegram / xAI hygiene."""

    window_seconds: float = 3600.0
    _events: Deque[Tuple[float, str, str]] = field(default_factory=deque)  # ts, kind, source
    _trip_count: int = 0
    last_score: int = 0
    last_band: str = "LOW"
    last_reasons: List[str] = field(default_factory=list)

    def _prune(self) -> None:
        cutoff = time.time() - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def note(self, kind: str, source: str = "") -> None:
        self._events.append((time.time(), kind, source or ""))
        self._prune()
        if kind == "breaker_trip":
            self._trip_count += 1

    def note_429(self, source: str = "kraken") -> None:
        self.note("429", source)

    def note_success(self, source: str = "kraken") -> None:
        self.note("ok", source)

    def note_breaker_trip(self) -> None:
        self.note("breaker_trip", "breaker")

    def note_backoff(self, source: str = "") -> None:
        self.note("backoff", source)

    def counts(self) -> Dict[str, int]:
        self._prune()
        out: Dict[str, int] = {}
        for _, kind, _ in self._events:
            out[kind] = out.get(kind, 0) + 1
        return out

    def evaluate(
        self,
        *,
        settings: Any,
        paper: bool,
        breaker: Any = None,
    ) -> Dict[str, Any]:
        """Return score 0–100 (higher = more risk), band, reasons, tips."""
        self._prune()
        c = self.counts()
        n429 = int(c.get("429", 0))
        n_trip = int(c.get("breaker_trip", 0))
        n_ok = int(c.get("ok", 0))
        cooling = bool(breaker is not None and breaker.cooling_down())

        score = 8  # baseline: any always-on trading bot has some footprint
        reasons: List[str] = []

        # Hygiene credits
        base_url = str(getattr(settings, "kraken_base_url", "") or "")
        xai_url = str(getattr(settings, "xai_base_url", "") or "")
        if "api.kraken.com" in base_url:
            score -= 3
            reasons.append("official Kraken REST ✓")
        else:
            score += 25
            reasons.append("non-standard Kraken base URL")

        if "api.x.ai" in xai_url or not getattr(settings, "xai_api_key", ""):
            score -= 2
            reasons.append("xAI official endpoint / unused ✓")
        elif getattr(settings, "xai_api_key", "") and "api.x.ai" not in xai_url:
            score += 20
            reasons.append("xAI base URL not api.x.ai")

        min_iv = float(getattr(settings, "kraken_public_min_interval", 0.2) or 0.2)
        if min_iv >= 0.2:
            score -= 4
            reasons.append(f"public throttle {min_iv:.2f}s ✓")
        elif min_iv < 0.1:
            score += 18
            reasons.append(f"aggressive throttle {min_iv:.2f}s")
        else:
            score += 6
            reasons.append(f"tight throttle {min_iv:.2f}s")

        if paper:
            score -= 5
            reasons.append("PAPER mode (no live AddOrder) ✓")
        else:
            score += 12
            reasons.append("LIVE mode — broker abuse flags matter more")

        # Bad signals
        if n429:
            add = min(40, 8 + n429 * 6)
            score += add
            reasons.append(f"{n429}× HTTP 429 in last hour (+{add})")
        if n_trip:
            add = min(25, n_trip * 10)
            score += add
            reasons.append(f"{n_trip}× rate-limit breaker trip(s) (+{add})")
        if cooling:
            score += 15
            rem = breaker.remaining_seconds() if breaker else 0
            from trading_bot.utils.countdown import format_countdown_duration

            rem_s = format_countdown_duration(rem)
            reasons.append(
                f"API rate-limit / 429 pause — time until trading starts again: {rem_s}"
            )

        # Recovery: many successes after issues
        if n429 and n_ok > n429 * 3:
            score -= 5
            reasons.append("recovering — successes >> 429s")

        score = int(max(0, min(100, score)))
        if score < 25:
            band = "LOW"
        elif score < 55:
            band = "MEDIUM"
        else:
            band = "HIGH"

        tips: List[str] = []
        if n429 or cooling:
            tips.append("Slow polls / raise KRAKEN_PUBLIC_MIN_INTERVAL; let cooldown finish.")
        if not paper:
            tips.append("Keep live order rate low; prefer maker/post-only; never retry storms.")
        if min_iv < 0.2:
            tips.append("Set KRAKEN_PUBLIC_MIN_INTERVAL≥0.2 and OHLC cache ≥8s.")
        if not tips:
            tips.append("Stay on official APIs, keep PAPER until armed, avoid 429 loops.")

        self.last_score = score
        self.last_band = band
        self.last_reasons = reasons
        return {
            "score": score,
            "band": band,
            "reasons": reasons,
            "tips": tips,
            "counts": {"429": n429, "breaker_trips": n_trip, "ok": n_ok},
            "cooling": cooling,
            "window_hours": self.window_seconds / 3600.0,
        }


def format_ban_risk_report(snap: Dict[str, Any]) -> str:
    band = snap.get("band", "?")
    score = int(snap.get("score") or 0)
    lines = [
        f"API ban-risk hygiene: {band} ({score}/100)",
        f"(heuristic · last {snap.get('window_hours', 1):.0f}h of our traffic — not a prediction)",
        "",
    ]
    for r in snap.get("reasons") or []:
        lines.append(f"• {r}")
    tips = snap.get("tips") or []
    if tips:
        lines.append("")
        lines.append("Tips:")
        for t in tips:
            lines.append(f"→ {t}")
    c = snap.get("counts") or {}
    lines.append("")
    lines.append(
        f"Window: 429={c.get('429', 0)} · breaker_trips={c.get('breaker_trips', 0)} · ok={c.get('ok', 0)}"
    )
    return "\n".join(lines)


def format_ban_risk_status_line(snap: Dict[str, Any]) -> str:
    icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(str(snap.get("band")), "⚪")
    return f"api_risk: {icon} {snap.get('band')} ({int(snap.get('score') or 0)}/100)"
