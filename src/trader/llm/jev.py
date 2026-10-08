"""TypeSafe's Jev as the confirm role (``TRADER_CONFIRM_PROVIDER=jev``).

Jev is a judgment model: it takes a state plus typed questions and returns probabilities,
and it writes no text. This client keeps the ``LLMClient`` shape so the registry routes and
meters it like any other role, with one difference: for Jev the ``schema`` argument IS the
question set (``{name: {"type": "choice" | "score", ...}}``), and the completion text is
Jev's answers as JSON, not a chat reply.

- the system message becomes Jev's ``state.rules`` and the last user message its
  ``state.prompt``;
- a JSON schema (anything that is not a question set) is refused, so Jev can never answer
  as a decider, an analyst or a research role even if it were routed there.

Errors report the HTTP status or the exception type only; response bodies are never echoed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.usage import price_per_million

JEV_PROVIDER = "jev"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
QUESTION_TYPES = frozenset({"choice", "score"})


def is_question_set(schema: dict[str, Any] | None) -> bool:
    """True for a Jev question set; False for a JSON schema or nothing."""
    return bool(schema) and all(
        isinstance(q, dict) and q.get("type") in QUESTION_TYPES for q in schema.values()
    )


class JevClient:
    """One Jev endpoint. Satisfies ``LLMClient``; answers question sets only."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        price_in: float = 0.0,
        price_out: float = 0.0,
    ) -> None:
        self.name = JEV_PROVIDER
        self.model = model
        self._url = base_url
        self._price_in = price_in
        self._price_out = price_out
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(timeout=timeout)

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
        del max_tokens, temperature  # Jev answers questions; it generates no tokens to cap
        if schema is None or not is_question_set(schema):
            raise LLMError(f"{self.name}: only answers a question set")
        prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        rules = next((m["content"] for m in messages if m.get("role") == "system"), "")
        body = {
            "model": self.model,
            "state": {"prompt": prompt, "rules": rules},
            "questions": schema,
        }
        try:
            resp = await self._client.post(self._url, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            # Status only: response bodies are not echoed into the journal or logs.
            raise LLMError(f"{self.name}: HTTP {resp.status_code}")
        try:
            payload = resp.json()
            answers = {name: payload["answers"][name] for name in schema}
            raw_usage = payload.get("usage") or {}
            usage = Usage(
                prompt_tokens=int(raw_usage.get("input_tokens", 0)),
                completion_tokens=int(raw_usage.get("output_tokens", 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"{self.name}: malformed answers: {type(exc).__name__}") from exc

        configured = (self._price_in, self._price_out)
        override = configured if configured != (0.0, 0.0) else None
        in_price, out_price = price_per_million(
            self.name, self.model, default=configured, override=override
        )
        cost = (usage.prompt_tokens * in_price + usage.completion_tokens * out_price) / 1_000_000
        return Completion(
            text=json.dumps(answers),
            usage=usage,
            cost_usd=cost,
            provider=self.name,
            model=self.model,
        )
