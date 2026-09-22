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


def format_countdown_clock(seconds: float) -> str:
    """Zero-padded clock remaining time: MM:SS or H:MM:SS."""
    try:
        s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def format_alarm_clock_countdown(seconds: float, *, bold: bool = True) -> str:
    """Loud clock motif: ⏰⏰   MM:SS   ⏰⏰ (no HTML — Telegram HTML parse often breaks /status)."""
    clock = format_countdown_clock(seconds)
    # bold flag kept for API compat; visual weight comes from alarm emojis + spacing
    _ = bold
    return f"⏰⏰   {clock}   ⏰⏰"


def format_trading_resume_countdown_line(
    seconds: float,
    *,
    kind: str = "",
    bold: bool = True,
) -> str:
    """User-facing pause countdown as a highly noticeable clock block.

    Example:
      ⏰⏰   <b>44:12</b>   ⏰⏰
      time until trading starts again
    """
    clock_line = format_alarm_clock_countdown(seconds, bold=bold)
    kind_s = str(kind or "").strip()
    if kind_s:
        caption = f"{kind_s} — time until trading starts again"
    else:
        caption = "time until trading starts again"
    return f"{clock_line}\n{caption}"
