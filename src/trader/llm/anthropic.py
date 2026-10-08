"""Native Anthropic adapter (#104): Claude models through the official ``anthropic`` SDK.

Satisfies ``LLMClient``. The system message goes to ``system`` and the rest to
``messages``; a JSON schema becomes structured output (``output_config.format``) after
``anthropic.transform_schema``, which moves the bounds the API does not take (lengths,
minimum/maximum) into descriptions and closes every object. Callers still validate the
reply with pydantic, so those bounds hold. No OpenAI-compatible shim for Claude.

The SDK is built on httpx2, not httpx: a test transport must be an ``httpx2`` one.
Errors carry status text only; an API error body can echo the request.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.usage import price_per_million

#: Default model for a new Anthropic Chain entry.
DEFAULT_MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 120.0


def sdk_base_url(base_url: str) -> str:
    """The SDK adds ``/v1`` itself; a Provider's URL carries it (``…/v1``)."""
    return base_url.rstrip("/").removesuffix("/v1")


def sdk_client(
    api_key: str, base_url: str, transport: httpx2.AsyncBaseTransport | None = None
) -> anthropic.AsyncAnthropic:
    # No SDK retries: a failing model falls through to the next Chain entry instead,
    # as every other client here does.
    http = anthropic.DefaultAsyncHttpxClient(transport=transport) if transport else None
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        base_url=sdk_base_url(base_url),
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
        http_client=http,
    )


def _status_text(name: str, exc: anthropic.APIError) -> LLMError:
    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"{name}: HTTP {exc.status_code}")
    return LLMError(f"{name}: unreachable ({type(exc).__name__})")


async def list_models(
    api_key: str, base_url: str, transport: httpx2.AsyncBaseTransport | None = None
) -> list[str]:
    """Model ids from the Models API (auto-paginated)."""
    client = sdk_client(api_key, base_url, transport)
    try:
        return [model.id async for model in client.models.list()]
    except anthropic.APIError as exc:
        raise _status_text("anthropic", exc) from None
    finally:
        await client.close()


class AnthropicClient:
    """One Claude model behind one Anthropic Provider."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str,
        base_url: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        price_in: float = 0.0,
        price_out: float = 0.0,
    ) -> None:
        self.name = name
        self.model = model
        self._price_in = price_in
        self._price_out = price_out
        self._client = sdk_client(api_key, base_url, transport)

    async def close(self) -> None:
        await self._client.close()

    def price(self) -> tuple[float, float]:
        """USD per 1M (prompt, completion) tokens, exactly as this client meters them."""
        configured = (self._price_in, self._price_out)
        override = configured if configured != (0.0, 0.0) else None
        return price_per_million(self.name, self.model, default=configured, override=override)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion:
        # temperature is not sent: current Claude models think adaptively, and a
        # non-default temperature is refused alongside thinking.
        del temperature
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        chat = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") != "system"
        ]
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": chat}
        if system:
            kwargs["system"] = system
        if schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
            }
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            # `from None`: the SDK error's message is the response body.
            raise _status_text(self.name, exc) from None
        if response.stop_reason in ("max_tokens", "refusal"):
            raise LLMError(f"{self.name}: reply ended early ({response.stop_reason})")

        text = "".join(block.text for block in response.content if block.type == "text")
        raw = response.usage
        usage = Usage(
            prompt_tokens=raw.input_tokens
            + (raw.cache_creation_input_tokens or 0)
            + (raw.cache_read_input_tokens or 0),
            completion_tokens=raw.output_tokens,
        )
        in_price, out_price = self.price()
        cost = (usage.prompt_tokens * in_price + usage.completion_tokens * out_price) / 1_000_000
        return Completion(
            text=text, usage=usage, cost_usd=cost, provider=self.name, model=self.model
        )
