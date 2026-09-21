"""Official xAI API only — sentiment/signal analyzer. Throttle + exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)


class GrokClient:
    """OpenAI-compatible client at https://api.x.ai/v1 with XAI_API_KEY.

    Never browser/consumer chat automation. AI is analyzer-only — no orders.
    Uses httpx against the official chat/completions endpoint (stable).
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = "https://api.x.ai/v1",
        model: str = "grok-2-latest",
        min_interval_seconds: float = 2.0,
    ):
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.min_interval = min_interval_seconds
        self._last_call = 0.0
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("YOUR_")

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "ApexSignalsNow/2.0 (+xai-official)",
            },
            timeout=60.0,
        )
        return self._client

    async def analyze_sentiment(self, prompt: str) -> str:
        """Sentiment/signal text only — never places orders."""
        if not self.configured:
            return "Grok not configured (set XAI_API_KEY for official api.x.ai)."

        await self._throttle()
        client = self._ensure_client()

        async def _do() -> str:
            r = await client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a crypto market sentiment analyzer for Apex Signals Now. "
                                "Provide brief sentiment only. Never suggest placing orders."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                },
            )
            if r.status_code == 403:
                detail = ""
                try:
                    detail = r.json().get("error") or r.text
                except Exception:  # noqa: BLE001
                    detail = r.text
                return (
                    "Grok API 403: team has no credits/licenses yet. "
                    f"Add billing at https://console.x.ai — {detail}"
                )
            if r.status_code == 429:
                r.raise_for_status()  # backoff retry
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip()

        try:
            return await with_exponential_backoff(_do, label="xai_grok")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Grok analyze failed: %s", exc)
            return f"Grok error: {exc}"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
