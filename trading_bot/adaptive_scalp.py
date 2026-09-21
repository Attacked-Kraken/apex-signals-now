"""Smart memory / quick-scalp threshold tighten (aggressive only)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class SmartMemory:
    """Rolling last 5 outcomes; winrate < 40% → tighten 35→55; 3 consec wins restore."""

    def __init__(self, path: str = "data/trade_memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        return {"outcomes": [], "tightened": False}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def record(self, won: bool) -> None:
        outcomes: List[bool] = list(self._data.get("outcomes") or [])
        outcomes.append(bool(won))
        outcomes = outcomes[-5:]
        self._data["outcomes"] = outcomes
        if len(outcomes) >= 5:
            wr = sum(1 for o in outcomes if o) / len(outcomes)
            if wr < 0.40:
                self._data["tightened"] = True
        if self._data.get("tightened"):
            # 3 consecutive wins while tightened → restore
            if len(outcomes) >= 3 and all(outcomes[-3:]):
                self._data["tightened"] = False
        self._save()

    def effective_threshold(self, base: float, *, wants_quick_scalp: bool) -> float:
        if not wants_quick_scalp:
            return base
        if self._data.get("tightened") and base <= 35.0:
            return 55.0
        return base

    def status_note(self) -> str:
        if not self._data.get("tightened"):
            return ""
        outcomes = self._data.get("outcomes") or []
        wins = sum(1 for o in outcomes if o)
        return f"(smart-memory tightened ({wins}/{len(outcomes)} wins))"
