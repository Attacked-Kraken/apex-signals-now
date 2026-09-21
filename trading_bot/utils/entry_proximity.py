"""Entry proximity / progress bars for /status (production Instance #2)."""
from __future__ import annotations

import os


def make_progress_bar(pct: float, width: int = 10) -> str:
    """Return bar body (no brackets) using production ⬛/⬜ blocks."""
    try:
        clamped = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        clamped = 0.0
    filled = int(round((clamped / 100.0) * width))
    filled = max(0, min(width, filled))
    return "⬛" * filled + "⬜" * (width - filled)


def get_entry_threshold() -> float:
    """Default entry threshold from env (callers usually pass explicit value)."""
    try:
        return float(os.environ.get("ENTRY_THRESHOLD", "60"))
    except (TypeError, ValueError):
        return 60.0


def proximity_bar(score: float, threshold: float, width: int = 10) -> str:
    """Legacy helper — prefer make_progress_bar + explicit score%."""
    _ = threshold
    try:
        pct = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        pct = 0.0
    return f"[{make_progress_bar(pct, width)}] {pct:.0f}%"


def progress_bar(pct: float, width: int = 10) -> str:
    """PnL progress toward TP (0–100). Returns bracketed bar."""
    return f"[{make_progress_bar(pct, width)}]"
