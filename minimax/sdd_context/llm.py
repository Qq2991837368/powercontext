"""MiniMax chat-completions client (OpenAI-compatible endpoint)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"


class MiniMaxError(RuntimeError):
    pass


class MiniMaxClient:
    def __init__(self) -> None:
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise MiniMaxError("MINIMAX_API_KEY is not set; export it in the shell before running `solve`.")
        self.api_key = api_key
        self.base_url = os.environ.get("MINIMAX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
        self.endpoint = f"{self.base_url}/chat/completions"

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 6144) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                    response = await client.post(self.endpoint, headers=headers, json=payload)
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = MiniMaxError(f"MiniMax transient HTTP {response.status_code}")
                    await asyncio.sleep(2 * attempt)
                    continue
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise MiniMaxError("MiniMax returned no choices")
                message = choices[0].get("message") or {}
                return {
                    "content": message.get("content") or "",
                    "reasoning_content": message.get("reasoning_content") or "",
                    "usage": data.get("usage") or {},
                    "response_id": data.get("id"),
                    "model": data.get("model", self.model),
                    "finish_reason": choices[0].get("finish_reason"),
                    "endpoint": self.endpoint,
                }
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                await asyncio.sleep(2 * attempt)
        raise MiniMaxError(f"MiniMax request failed after retries: {last_error}")
