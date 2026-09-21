"""Official xAI API only — sentiment/signal analyzer. Throttle + exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from trading_bot.utils.retry import with_exponential_backoff

logger = logging.getLogger(__name__)


class GrokClient:
    """Uses OpenAI-compatible client at https://api.x.ai/v1 with XAI_API_KEY.
    Never browser/consumer chat automation. AI is analyzer-only — no orders.
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
        # Prefer xai_sdk; fall back to OpenAI-compatible httpx
        try:
            from xai_sdk import Client  # type: ignore

            self._client = ("xai_sdk", Client(api_key=self.api_key))
            return self._client
        except Exception:  # noqa: BLE001
            pass
        try:
            from openai import AsyncOpenAI  # type: ignore

            self._client = (
                "openai",
                AsyncOpenAI(api_key=self.api_key, base_url=self.base_url),
            )
            return self._client
        except Exception:  # noqa: BLE001
            import httpx

            self._client = ("httpx", httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "ApexSignalsNow/2.0 (+xai-official)",
                },
                timeout=60.0,
            ))
            return self._client

    async def analyze_sentiment(self, prompt: str) -> str:
        """Sentiment/signal text only — never places orders."""
        if not self.configured:
            return "Grok not configured (set XAI_API_KEY for official api.x.ai)."

        await self._throttle()
        kind, client = self._ensure_client()

        async def _do() -> str:
            if kind == "openai":
                resp = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a crypto market sentiment analyzer for Apex Signals Now. "
                                "Provide brief sentiment only. Never suggest placing orders."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=400,
                )
                return (resp.choices[0].message.content or "").strip()
            if kind == "httpx":
                r = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a crypto market sentiment analyzer. "
                                    "Brief sentiment only. Never place orders."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 400,
                    },
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
            # xai_sdk sync bridge
            def _sync() -> str:
                # Best-effort; SDK shapes vary
                chat = client.chat.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return str(getattr(chat, "content", chat))

            return await asyncio.to_thread(_sync)

        try:
            return await with_exponential_backoff(_do, label="xai_grok")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Grok analyze failed: %s", exc)
            return f"Grok error: {exc}"

    async def close(self) -> None:
        if self._client and self._client[0] == "httpx":
            await self._client[1].aclose()
        self._client = None
