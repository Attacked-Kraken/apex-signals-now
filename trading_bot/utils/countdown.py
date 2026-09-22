"""Human-readable remaining-time helpers for trading / API pauses."""
from __future__ import annotations


def format_countdown_duration(seconds: float) -> str:
    """Human-readable remaining time (e.g. 87s, 12m 05s, 1h 05m)."""
    try:
        s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m" if m else f"{h}h"
    if m:
        return f"{m}m {sec:02d}s" if sec else f"{m}m"
    return f"{sec}s"


def format_trading_resume_countdown_line(
    seconds: float,
    *,
    kind: str = "",
    bold: bool = True,
) -> str:
    """User-facing pause countdown; TIME is bold via Telegram HTML <b> when bold=True."""
    t = format_countdown_duration(seconds)
    time_s = f"<b>{t}</b>" if bold else t
    kind_s = str(kind or "").strip()
    if kind_s:
        return f"⏱ {kind_s} — time until trading starts again: {time_s}"
    return f"⏱ time until trading starts again: {time_s}"
