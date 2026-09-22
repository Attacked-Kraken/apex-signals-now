"""LIVE safety tracker — paper-compatible, enforces only when live.

Tracks consecutive private-auth failures, connectivity/private timeouts,
and stale ticker age while positions are open. Alerts + entry pause fire
in LIVE only; exits remain attempted.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

ALERT_AUTH = "🚨 LIVE AUTH FAILURE — open positions require attention"
ALERT_CONNECTIVITY = "🚨 LIVE CONNECTIVITY LOST — open positions may be unmanaged"
ALERT_STALE_PREFIX = "🚨 LIVE STALE MARKS"

KIND_AUTH = "auth"
KIND_CONNECTIVITY = "connectivity"
KIND_STALE = "stale"

AUTH_FAIL_THRESHOLD = 2
CONNECTIVITY_FAIL_THRESHOLD = 3
STALE_TICKER_SECONDS = 45.0
ALERT_COOLDOWN_SECONDS = 300.0  # 5 minutes
DEADMAN_TIMEOUT_SEC = 60
DEADMAN_NOT_ARMED = "not armed — private live path incomplete"
DEADMAN_PAPER = "not armed — paper mode"
DEADMAN_ARMED = "armed — CancelAllOrdersAfter 60s"

_AUTH_MARKERS = (
    "eapi:invalid key",
    "eapi:invalid signature",
    "eapi:invalid permission",
    "egeneral:permission denied",
    "invalid key",
    "invalid signature",
    "invalid permission",
    "permission denied",
)
_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "connecterror",
    "connect timeout",
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "networkerror",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "name or service not known",
    "failed to establish a new connection",
)
_SECRET_KEYS = ("token", "secret", "key", "password", "authorization", "api_key", "api_secret")


def _now(now: Optional[float] = None) -> float:
    return time.time() if now is None else float(now)


def _text_of(exc: Any) -> str:
    if exc is None:
        return ""
    if isinstance(exc, str):
        return exc
    parts = [str(exc), type(exc).__name__]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            parts.append(str(getattr(resp, "text", "") or ""))
        except Exception:  # noqa: BLE001
            pass
    return " ".join(p for p in parts if p)


def _sanitize_error(text: str) -> str:
    """Redact credential-like values before status/Telegram display."""
    out = str(text or "")
    out = re.sub(
        r"(?i)(api[_\s-]?(?:key|secret)|authorization|bearer|token|password)"
        r"(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        out,
    )
    out = re.sub(r"(api\.telegram\.org/bot)\d+:[A-Za-z0-9_-]+", r"\1[REDACTED]", out)
    return out[:240]


def _status_of(exc: Any, status_code: Optional[int] = None) -> Optional[int]:
    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError):
            pass
    if isinstance(exc, int):
        return int(exc)
    sc = getattr(exc, "status_code", None)
    if sc is not None:
        try:
            return int(sc)
        except (TypeError, ValueError):
            pass
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc is not None:
            try:
                return int(sc)
            except (TypeError, ValueError):
                pass
    return None


def classify_kraken_error(
    exc: Any = None,
    *,
    status_code: Optional[int] = None,
    body: Optional[str] = None,
) -> str:
    """Return 'auth' | 'timeout' | 'other' for a Kraken/HTTP failure."""
    status = _status_of(exc, status_code)
    if status in (401, 403):
        return KIND_AUTH
    blob = f"{_text_of(exc)} {body or ''}".lower()
    if any(m in blob for m in _AUTH_MARKERS):
        return KIND_AUTH
    # httpx timeout / connect types
    name = type(exc).__name__.lower() if exc is not None and not isinstance(exc, (str, int)) else ""
    if "timeout" in name or name in {"connecterror", "networkerror", "remoteprotocolerror"}:
        return KIND_CONNECTIVITY
    if any(m in blob for m in _TIMEOUT_MARKERS):
        return KIND_CONNECTIVITY
    return "other"


def format_dead_man_status(
    *,
    paper: bool,
    signing_available: bool = False,
    private_path_complete: bool = False,
) -> str:
    """Honest dead-man status — never reports armed without a signed live path."""
    if paper:
        return DEADMAN_PAPER
    if signing_available and private_path_complete:
        return DEADMAN_ARMED
    return DEADMAN_NOT_ARMED


def _fmt_ago(ts: float, now: Optional[float] = None) -> str:
    if not ts:
        return "never"
    age = max(0.0, _now(now) - float(ts))
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60.0:.1f}m ago"
    return f"{age / 3600.0:.1f}h ago"


@dataclass
class LiveSafetyTracker:
    """In-memory consecutive-failure / stale-mark tracker + alert dedupe."""

    auth_fail_threshold: int = AUTH_FAIL_THRESHOLD
    connectivity_fail_threshold: int = CONNECTIVITY_FAIL_THRESHOLD
    stale_seconds: float = STALE_TICKER_SECONDS
    alert_cooldown_seconds: float = ALERT_COOLDOWN_SECONDS

    consecutive_auth_failures: int = 0
    consecutive_timeouts: int = 0
    last_auth_ok_ts: float = 0.0
    last_connectivity_ok_ts: float = 0.0
    last_ticker_ok_ts: float = 0.0
    last_private_ok_ts: float = 0.0
    last_auth_error: str = ""
    last_connectivity_error: str = ""
    last_alert_ts: Dict[str, float] = field(default_factory=dict)
    last_ticker_symbol: str = ""

    def note_auth_ok(self, now: Optional[float] = None) -> None:
        self.consecutive_auth_failures = 0
        self.last_auth_ok_ts = _now(now)
        self.last_auth_error = ""

    def note_private_ok(self, now: Optional[float] = None) -> None:
        """Private API success — auth + connectivity + private freshness."""
        ts = _now(now)
        self.note_auth_ok(now=ts)
        self.note_connectivity_ok(now=ts)
        self.last_private_ok_ts = ts

    def note_auth_failure(self, reason: str = "", now: Optional[float] = None) -> None:
        self.consecutive_auth_failures += 1
        self.last_auth_error = (reason or "auth failure")[:240]

    def note_connectivity_ok(self, now: Optional[float] = None) -> None:
        self.consecutive_timeouts = 0
        self.last_connectivity_ok_ts = _now(now)
        self.last_connectivity_error = ""

    def note_timeout(self, reason: str = "", now: Optional[float] = None) -> None:
        self.consecutive_timeouts += 1
        self.last_connectivity_error = (reason or "timeout")[:240]

    def note_ticker_ok(self, symbol: str = "", now: Optional[float] = None) -> None:
        ts = _now(now)
        self.last_ticker_ok_ts = ts
        self.last_ticker_symbol = symbol or self.last_ticker_symbol
        self.note_connectivity_ok(now=ts)

    def note_exception(
        self,
        exc: Any,
        *,
        source: str = "",
        status_code: Optional[int] = None,
        now: Optional[float] = None,
    ) -> str:
        kind = classify_kraken_error(exc, status_code=status_code)
        raw_reason = f"{source}: {_text_of(exc)}" if source else _text_of(exc)
        reason = _sanitize_error(raw_reason)
        if kind == KIND_AUTH:
            self.note_auth_failure(reason, now=now)
        elif kind == KIND_CONNECTIVITY:
            self.note_timeout(reason, now=now)
        return kind

    def auth_failed(self) -> bool:
        return self.consecutive_auth_failures >= int(self.auth_fail_threshold)

    def connectivity_lost(self) -> bool:
        return self.consecutive_timeouts >= int(self.connectivity_fail_threshold)

    def ticker_age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if not self.last_ticker_ok_ts:
            return None
        return max(0.0, _now(now) - float(self.last_ticker_ok_ts))

    def stale_marks(self, *, positions_open: bool, now: Optional[float] = None) -> bool:
        if not positions_open:
            return False
        age = self.ticker_age_seconds(now)
        if age is None:
            # Never marked a ticker while positions are open → treat as stale
            return True
        return age >= float(self.stale_seconds)

    def entries_paused(
        self, *, live: bool, positions_open: bool, now: Optional[float] = None
    ) -> bool:
        if not live:
            return False
        if self.auth_failed() or self.connectivity_lost():
            return True
        return self.stale_marks(positions_open=positions_open, now=now)

    def entry_block_reason(
        self, *, live: bool, positions_open: bool, now: Optional[float] = None
    ) -> Optional[str]:
        if not self.entries_paused(live=live, positions_open=positions_open, now=now):
            return None
        if self.auth_failed():
            return "live_safety: AUTH FAILURE — new entries paused"
        if self.connectivity_lost():
            return "live_safety: CONNECTIVITY LOST — new entries paused"
        if self.stale_marks(positions_open=positions_open, now=now):
            age = self.ticker_age_seconds(now)
            age_s = f"{age:.0f}s" if age is not None else "unknown"
            return f"live_safety: STALE MARKS ({age_s}) — new entries paused"
        return "live_safety: new entries paused"

    def should_alert(self, kind: str, *, now: Optional[float] = None) -> bool:
        last = float(self.last_alert_ts.get(kind) or 0.0)
        if last <= 0:
            return True
        return (_now(now) - last) >= float(self.alert_cooldown_seconds)

    def mark_alerted(self, kind: str, now: Optional[float] = None) -> None:
        self.last_alert_ts[kind] = _now(now)

    def consume_alerts(
        self,
        *,
        live: bool,
        positions_open: bool,
        now: Optional[float] = None,
    ) -> List[str]:
        """Return alert strings to send. LIVE + open positions only; 5m dedupe."""
        if not live or not positions_open:
            return []
        ts = _now(now)
        out: List[str] = []
        if self.auth_failed() and self.should_alert(KIND_AUTH, now=ts):
            out.append(ALERT_AUTH)
            self.mark_alerted(KIND_AUTH, now=ts)
        if self.connectivity_lost() and self.should_alert(KIND_CONNECTIVITY, now=ts):
            out.append(ALERT_CONNECTIVITY)
            self.mark_alerted(KIND_CONNECTIVITY, now=ts)
        if self.stale_marks(positions_open=True, now=ts) and self.should_alert(KIND_STALE, now=ts):
            age = self.ticker_age_seconds(ts)
            age_s = f"{age:.0f}s" if age is not None else "unknown"
            out.append(
                f"{ALERT_STALE_PREFIX} — ticker age {age_s} while "
                f"positions open (gate {self.stale_seconds:.0f}s)"
            )
            self.mark_alerted(KIND_STALE, now=ts)
        return out

    def snapshot(
        self,
        *,
        live: bool,
        positions_open: int = 0,
        dead_man: str = DEADMAN_NOT_ARMED,
        heartbeat_path: Optional[Union[str, Path]] = None,
        pid: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        n_pos = int(positions_open)
        open_flag = n_pos > 0
        ts = _now(now)
        age = self.ticker_age_seconds(ts)
        paused = self.entries_paused(live=live, positions_open=open_flag, now=ts)
        return {
            "live": bool(live),
            "mode": "LIVE" if live else "PAPER",
            "positions_open": n_pos,
            "entries_paused": paused,
            "entry_block_reason": self.entry_block_reason(
                live=live, positions_open=open_flag, now=ts
            ),
            "auth_failed": self.auth_failed(),
            "connectivity_lost": self.connectivity_lost(),
            "stale_marks": self.stale_marks(positions_open=open_flag, now=ts),
            "consecutive_auth_failures": int(self.consecutive_auth_failures),
            "consecutive_timeouts": int(self.consecutive_timeouts),
            "last_auth_ok": _fmt_ago(self.last_auth_ok_ts, ts),
            "last_connectivity_ok": _fmt_ago(self.last_connectivity_ok_ts, ts),
            "last_ticker_ok": _fmt_ago(self.last_ticker_ok_ts, ts),
            "last_private_ok": _fmt_ago(self.last_private_ok_ts, ts),
            "ticker_age_seconds": age,
            "stale_gate_seconds": float(self.stale_seconds),
            "last_auth_error": self.last_auth_error,
            "last_connectivity_error": self.last_connectivity_error,
            "dead_man": dead_man,
            "heartbeat_path": str(heartbeat_path) if heartbeat_path else "",
            "pid": int(pid) if pid is not None else os.getpid(),
            "alert_cooldown_seconds": float(self.alert_cooldown_seconds),
        }


def format_live_safety_report(snap: Dict[str, Any]) -> str:
    live = bool(snap.get("live"))
    n_pos = int(snap.get("positions_open") or 0)
    paused = bool(snap.get("entries_paused"))
    auth_s = "FAIL" if snap.get("auth_failed") else "OK"
    conn_s = "FAIL" if snap.get("connectivity_lost") else "OK"
    stale_s = "FAIL" if snap.get("stale_marks") else "OK"
    age = snap.get("ticker_age_seconds")
    age_s = f"{float(age):.0f}s" if age is not None else "n/a"
    lines = [
        "🛡 LIVE safety",
        f"mode: {snap.get('mode', 'PAPER')} "
        f"({'enforcing' if live else 'paper — track only, no pause/alerts'})",
        f"positions: {n_pos} open",
        f"entries: {'PAUSED' if paused else 'allowed (live-only pause)'}"
        + (f" — {snap.get('entry_block_reason')}" if paused and snap.get("entry_block_reason") else ""),
        f"auth: {auth_s}  (consec={int(snap.get('consecutive_auth_failures') or 0)}, "
        f"last OK {snap.get('last_auth_ok', 'never')})",
        f"connectivity: {conn_s}  (consec={int(snap.get('consecutive_timeouts') or 0)}, "
        f"last OK {snap.get('last_connectivity_ok', 'never')})",
        f"stale marks: {stale_s}  (ticker age {age_s}, gate "
        f"{float(snap.get('stale_gate_seconds') or STALE_TICKER_SECONDS):.0f}s, "
        f"only while positions open)",
        f"last private OK: {snap.get('last_private_ok', 'never')}",
        f"dead-man: {snap.get('dead_man') or DEADMAN_NOT_ARMED}",
    ]
    hb = snap.get("heartbeat_path") or "data/bot_heartbeat.json"
    lines.append(f"heartbeat: {hb} (pid {snap.get('pid') or '?'})")
    lines.append(
        f"alerts: live+open-positions only; {int(float(snap.get('alert_cooldown_seconds') or 300)) // 60}m cooldown"
    )
    if snap.get("last_auth_error"):
        lines.append(f"last auth error: {snap['last_auth_error']}")
    if snap.get("last_connectivity_error"):
        lines.append(f"last connectivity error: {snap['last_connectivity_error']}")
    return "\n".join(lines)


def default_heartbeat_path(settings: Any = None) -> Path:
    if settings is not None:
        custom = getattr(settings, "bot_heartbeat_path", None)
        if custom:
            return Path(str(custom))
        paper = getattr(settings, "paper_book_path", None)
        if paper:
            return Path(str(paper)).expanduser().resolve().parent / "bot_heartbeat.json"
    return Path(__file__).resolve().parents[2] / "data" / "bot_heartbeat.json"


def write_bot_heartbeat(
    path: Optional[Union[str, Path]] = None,
    *,
    mode: str,
    pid: Optional[int] = None,
    open_positions: int = 0,
    extra: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Token-free supervisor heartbeat. Never writes secrets/tokens/keys."""
    dest = Path(path) if path else default_heartbeat_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = _now(now)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "ts": iso,
        "unix": ts,
        "mode": "LIVE" if str(mode).upper() == "LIVE" else "PAPER",
        "pid": int(pid if pid is not None else os.getpid()),
        "open_positions": int(open_positions),
    }
    if extra:
        for k, v in extra.items():
            key = str(k)
            if any(s in key.lower() for s in _SECRET_KEYS):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                payload[key] = v
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(dest)
    return payload
