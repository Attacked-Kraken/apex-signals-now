"""Entry proximity bar for /status."""
from __future__ import annotations


def proximity_bar(score: float, threshold: float, width: int = 10) -> str:
    """Return e.g. `[████░░░░░░] 42%` relative to threshold."""
    if threshold <= 0:
        pct = min(100.0, max(0.0, score))
    else:
        pct = min(100.0, max(0.0, (score / threshold) * 100.0))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.0f}%"


def progress_bar(pct: float, width: int = 10) -> str:
    """PnL progress toward TP (0–100+)."""
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"
