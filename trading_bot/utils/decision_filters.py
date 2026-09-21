"""Fee-aware trailing / fee_lock helpers (archive §3)."""
from __future__ import annotations

from typing import Optional

FEE_BUFFER = 0.0085  # default 0.85%
TRAIL_FEE_BUFFER_PCT = 0.0085
FEE_BUFFER_MIN = 0.0085
FEE_BUFFER_MAX = 0.012


def clamp_fee_buffer(pct: float) -> float:
    return max(FEE_BUFFER_MIN, min(FEE_BUFFER_MAX, float(pct)))


def fee_lock_sl(entry: float, *, short: bool = False, fee_buffer_pct: float = FEE_BUFFER) -> float:
    buf = clamp_fee_buffer(fee_buffer_pct)
    if short:
        return entry * (1.0 - buf)
    return entry * (1.0 + buf)


def maybe_fee_lock_sl(
    entry: float,
    mark: float,
    current_sl: Optional[float],
    *,
    short: bool = False,
    arm_pct: Optional[float] = None,
    fee_buffer_pct: Optional[float] = None,
) -> Optional[float]:
    """Arm fee-lock SL only if UPL >= arm (and arm >= buffer). Raise long SL to fee floor."""
    buf = clamp_fee_buffer(fee_buffer_pct if fee_buffer_pct is not None else FEE_BUFFER)
    arm = float(arm_pct) if arm_pct is not None else buf
    arm = max(arm, buf)

    if entry <= 0:
        return current_sl

    if short:
        upl = (entry - mark) / entry
    else:
        upl = (mark - entry) / entry

    if upl < arm:
        return current_sl

    floor = fee_lock_sl(entry, short=short, fee_buffer_pct=buf)
    if current_sl is None:
        return floor
    if short:
        # for shorts, SL is above entry; fee floor is lower (better)
        return min(current_sl, floor)
    return max(current_sl, floor)


def compute_fee_band(maker_fee: float = 0.0025, taker_fee: float = 0.004, pad: float = 0.001) -> float:
    """Computed band from fees + pad, clamped [0.0085, 0.012]."""
    raw = maker_fee + taker_fee + pad
    return clamp_fee_buffer(raw)
