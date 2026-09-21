"""HTTP rate-limit helpers (Retry-After / 429 / 503)."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any, Optional


class RateLimitError(Exception):
    """Raised when an upstream responds with 429 or Retry-After-bearing 503."""

    def __init__(
        self,
        message: str = "rate limited",
        *,
        retry_after: Optional[float] = None,
        status_code: int = 429,
        label: str = "",
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = int(status_code)
        self.label = label or ""


def retry_after_seconds(response: Any) -> Optional[float]:
    """Parse Retry-After header as delay-seconds or HTTP-date; return float seconds or None."""
    try:
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        import time as _time

        if dt.tzinfo is None:
            # Treat naive as UTC per RFC 7231 common practice
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, float(dt.timestamp() - _time.time()))
    except Exception:  # noqa: BLE001
        return None


def raise_for_rate_limit(response: Any, *, label: str = "") -> None:
    """Raise RateLimitError on 429 (always) or 503 with Retry-After."""
    status = int(getattr(response, "status_code", 0) or 0)
    ra = retry_after_seconds(response)
    if status == 429:
        raise RateLimitError(
            f"HTTP 429 rate limited{f' ({label})' if label else ''}",
            retry_after=ra,
            status_code=429,
            label=label,
        )
    if status == 503 and ra is not None:
        raise RateLimitError(
            f"HTTP 503 with Retry-After{f' ({label})' if label else ''}",
            retry_after=ra,
            status_code=503,
            label=label,
        )
