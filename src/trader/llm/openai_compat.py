"""Generic OpenAI-compatible chat client (vLLM, xAI, anything speaking /chat/completions).

Providers differ only in base URL, auth, price, and provider-specific extra body fields —
the Nemotron and xAI clients are thin factories over this.
"""

from __future__ import annotations

from typing import Any

import httpx

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.usage import price_per_million


class OpenAIChatClient:
    """One provider+model over the OpenAI chat-completions wire format."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.model = model
        self._extra_body = extra_body or {}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self._extra_body,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: {exc}") from exc

        try:
            text = payload["choices"][0]["message"]["content"]
            raw_usage = payload["usage"]
            usage = Usage(
                prompt_tokens=int(raw_usage["prompt_tokens"]),
                completion_tokens=int(raw_usage["completion_tokens"]),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: malformed completion payload: {exc}") from exc

        in_price, out_price = price_per_million(self.name, self.model)
        cost = (usage.prompt_tokens * in_price + usage.completion_tokens * out_price) / 1_000_000
        return Completion(
            text=text or "", usage=usage, cost_usd=cost, provider=self.name, model=self.model
        )
