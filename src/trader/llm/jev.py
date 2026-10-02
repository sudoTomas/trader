"""TypeSafe's Jev as a decide provider (``TRADER_DECIDE_PROVIDER=jev``).

Jev is a judgment model: it takes a state plus typed questions and returns probabilities,
and it writes no text. This client speaks the decide role's contract anyway:

- the decide prompt becomes Jev's ``state.prompt`` and the system prompt (which carries
  the exit-review rules when a position is held) becomes ``state.rules``;
- five questions go out in one call: the action, the side for an open, the side for a
  close, what the operator's research (the Macro View) favors, and how strong the edge is;
- the answers become a DecisionOutput JSON object. Conviction is the probability Jev gives
  the chosen action; the rationale is Jev's judgments written out with their probabilities.

Sizing stays with RiskGuard (Kelly ignores conviction), so opens and closes carry a nominal
``TRADER_JEV_NOTIONAL_USD`` that RiskGuard clamps; closes are sized from the broker's
position anyway.

Research-led open (``TRADER_JEV_RESEARCH_LED_OPEN``, dark): Jev's own action question
rarely says open. With the switch on, when nothing is held, the Macro View favors long
and Jev sees at least a weak edge, the client opens a long itself. Shorts and closes still
need Jev's own vote.

Only the decide schema is served. Analyst and research roles never route here.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.usage import price_per_million

JEV_PROVIDER = "jev"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DECISION_KEYS = frozenset({"action", "conviction", "rationale", "side", "notional_usd"})

EDGE_LEVELS = ("none", "weak", "moderate", "strong")
MIN_OPEN_EDGE = EDGE_LEVELS.index("weak")
POSITION_RE = re.compile(r"^Open position: (\S+) units", re.MULTILINE)

QUESTIONS: dict[str, Any] = {
    "action": {
        "type": "choice",
        "instructions": (
            "You are the decision step of a risk-guarded trading agent. Read the decide "
            "prompt in `prompt` and the agent's operating rules in `rules`, then choose what "
            "the agent should do now for the symbol in the prompt. Deterministic risk code "
            "sizes and vets any trade afterwards; your job is only the intent."
        ),
        "criteria": {
            "hold": (
                "Do nothing now: no defensible edge, the setup is unclear, or a held "
                "position's thesis still stands."
            ),
            "open": (
                "Open a new position: there is a clear, defensible edge and no position is "
                "currently held."
            ),
            "close": (
                "Close the held position: its thesis has failed or the edge is gone. Only "
                "valid when a position is held."
            ),
        },
    },
    "open_side": {
        "type": "choice",
        "instructions": (
            "If the agent opened a position on the symbol in `prompt` right now, which side has "
            "the better case?"
        ),
        "criteria": {
            "buy": "Long: the case for the price rising is stronger.",
            "sell": "Short: the case for the price falling is stronger.",
        },
    },
    "close_side": {
        "type": "choice",
        "instructions": (
            "Suppose the agent closes the position described in `prompt`. Which order side "
            "closes it?"
        ),
        "criteria": {
            "sell": "Sell: the held position is long, or no position is described.",
            "buy": "Buy: the held position is short.",
        },
    },
    "research": {
        "type": "choice",
        "instructions": (
            "The prompt in `prompt` may include an 'Operator Macro View' distilled from research "
            "emails. For the symbol this prompt is about, what does that research favor?"
        ),
        "criteria": {
            "long": "The research supports owning or buying this symbol.",
            "short": (
                "The research argues against this symbol, favoring selling, shorting or "
                "avoiding it."
            ),
            "silent": (
                "The research says nothing relevant about this symbol, or there is no Macro View."
            ),
        },
    },
    "edge": {
        "type": "score",
        "instructions": (
            "How strong and defensible is the trading edge in the data in `prompt` right now?"
        ),
        "criteria": [
            "No edge: neutral indicators, chop, nothing actionable.",
            "Weak: a faint lean that noise could easily erase.",
            "Moderate: a real lean supported by more than one signal.",
            "Strong: several independent signals agree and the risk is well defined.",
        ],
    },
}


def _pct(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100)}%"


def holds_position(prompt: str) -> bool:
    """True unless the decide prompt says the position is 0 units (unknown counts as held)."""
    match = POSITION_RE.search(prompt)
    if not match:
        return True
    try:
        return float(match.group(1)) != 0
    except ValueError:
        return True


def to_decision(
    answers: dict[str, Any], prompt: str, *, notional_usd: float, research_led_open: bool
) -> dict[str, Any]:
    """Jev's answers as a DecisionOutput-shaped dict."""
    action = answers["action"]
    choice = action["choice"]
    probs = action.get("probabilities") or {}
    edge = answers["edge"]
    edge_probs = edge.get("probabilities") or {}
    edge_level = max(edge_probs, key=edge_probs.get) if edge_probs else "0"
    research = answers.get("research") or {}
    r_choice = research.get("choice", "silent")
    research_led = (
        research_led_open
        and choice == "hold"
        and not holds_position(prompt)
        and r_choice == "long"
        and int(edge_level) >= MIN_OPEN_EDGE
    )
    side = None
    if research_led:
        side = "buy"
    elif choice == "open":
        side = answers["open_side"]["choice"]
    elif choice == "close":
        side = answers["close_side"]["choice"]

    others = ", ".join(
        f"{k} {_pct(v)}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]) if k != choice
    )
    lean = answers["open_side"]
    r_words = {
        "long": "favors long",
        "short": "argues against",
        "silent": "is silent on this symbol",
    }[r_choice]
    rationale = (
        f"Jev judged {choice} at {_pct(probs.get(choice))} ({others}). "
        f"Edge: {EDGE_LEVELS[int(edge_level)]} at {_pct(edge_probs.get(edge_level))} "
        f"(score {edge.get('score', 0):.2f}). "
        f"Side lean if opening: {lean['choice']} at "
        f"{_pct((lean.get('probabilities') or {}).get(lean['choice']))}. "
        f"Research (Macro View) {r_words} at "
        f"{_pct((research.get('probabilities') or {}).get(r_choice))}."
    )
    conviction = probs.get(choice, action.get("confidence", 0.0))
    if research_led:
        rationale = (
            "Research-led open (long): the Macro View favors long and the edge is at least "
            "weak. " + rationale
        )
        choice = "open"
        conviction = (research.get("probabilities") or {}).get("long", 0.0)
    return {
        "action": choice,
        "conviction": round(float(conviction), 4),
        "rationale": rationale,
        "side": side,
        "notional_usd": notional_usd if choice != "hold" else 0,
    }


class JevClient:
    """The decide role on Jev. Satisfies ``LLMClient``; refuses every other schema."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        notional_usd: float = 750.0,
        research_led_open: bool = False,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        price_in: float = 0.0,
        price_out: float = 0.0,
    ) -> None:
        self.name = JEV_PROVIDER
        self.model = model
        self._url = base_url
        self._notional = notional_usd
        self._research_led_open = research_led_open
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
        if schema is None or set(schema.get("properties") or {}) != DECISION_KEYS:
            raise LLMError(f"{self.name}: only serves the decide schema")
        prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        rules = next((m["content"] for m in messages if m.get("role") == "system"), "")
        body = {
            "model": self.model,
            "state": {"prompt": prompt, "rules": rules},
            "questions": QUESTIONS,
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
            decision = to_decision(
                payload["answers"],
                prompt,
                notional_usd=self._notional,
                research_led_open=self._research_led_open,
            )
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
            text=json.dumps(decision),
            usage=usage,
            cost_usd=cost,
            provider=self.name,
            model=self.model,
        )
