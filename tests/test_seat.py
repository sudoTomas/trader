"""Grok desk seat (#9) + sweep citation loop (#10).

Everything over ``httpx.MockTransport``: the xAI ``/v1/responses`` call and the trader API
(capture key) are both stubbed. Nothing here reaches api.x.ai or :8080. The rules under
test: zero citations = dropped (docs/lessons.md#sentiment-no-citations-is-unavailable),
dedupe against live notes before submitting, bodies over the size cap re-distilled once
then dropped, the capture key (never the operator key) on the write path, and the seat's
own cost written to an append-only local file.
"""

from __future__ import annotations

import configparser
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from trader.llm.base import Usage
from trader.ops.config import Settings
from trader.seats import SEATS
from trader.seats import runner as seat_runner
from trader.seats.crypto_flows import CRYPTO_FLOWS
from trader.seats.runner import (
    CandidateNote,
    CaptureClient,
    SeatRunner,
    SeatXaiClient,
    SweepCost,
    append_run,
    build_capture_http,
    build_xai_http,
    estimate_cost,
    ground,
    is_duplicate,
    parse_notes,
    parse_responses_payload,
    read_runs,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
CAPTURE = "capture-sekret"
OPERATOR = "operator-sekret"
XAI_BASE = "https://api.x.ai/v1"
API_BASE = "http://trader.test"

X_URL = "https://x.com/farside/status/1001"
WEB_URL = "https://www.coindesk.com/etf-flows"
OTHER_URL = "https://www.theblock.co/stablecoins"


def candidate(**overrides) -> dict:
    base = {
        "title": "Spot ETH ETFs saw $120m net outflows on Sep 28",
        "body": "Farside data: -$120m net across spot ETH ETFs on Sep 28; third outflow day.",
        "symbols": ["ETH/USD"],
        "tags": ["etf"],
        "source": WEB_URL,
        "authored_at": "2026-09-28T22:00:00+00:00",
        "confidence": 0.8,
        "citations": [WEB_URL, X_URL],
    }
    base.update(overrides)
    return base


def responses_payload(
    notes: list[dict] | str,
    citations: list[str],
    *,
    usage: tuple[int, int] = (1000, 200),
    tool_items: list[dict] | None = None,
) -> dict:
    text = notes if isinstance(notes, str) else json.dumps({"notes": notes})
    annotations = [{"type": "url_citation", "url": u} for u in citations[:1]]
    output = list(tool_items or [])
    output.append(
        {
            "type": "message",
            "content": [{"type": "output_text", "text": text, "annotations": annotations}],
        }
    )
    return {
        "id": "r1",
        "output": output,
        "citations": citations,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def xai_client(replies: list[dict], captured: list[dict], *, model: str = "grok-4.3"):
    """A SeatXaiClient whose ``/responses`` answers ``replies`` in order."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    http = httpx.AsyncClient(base_url=XAI_BASE, transport=httpx.MockTransport(handler))
    return SeatXaiClient(http, model=model)


class FakeApi:
    """The trader API as the seat sees it: notes list, note submit, macro view, usage."""

    def __init__(
        self,
        *,
        notes: list[dict] | None = None,
        view: dict | None = None,
        usage_total: float = 0.0,
    ) -> None:
        self.notes = list(notes or [])
        self.view = view
        self.usage_total = usage_total
        self.requests: list[httpx.Request] = []
        self.submitted: list[dict] = []
        self._next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.headers.get("x-api-key")
        path = request.url.path
        if path == "/research/notes" and request.method == "GET":
            if key != CAPTURE and key != OPERATOR:
                return httpx.Response(401, json={"detail": "unauthorized"})
            return httpx.Response(200, json=self.notes)
        if path == "/research/notes" and request.method == "POST":
            if key != CAPTURE and key != OPERATOR:
                return httpx.Response(401, json={"detail": "unauthorized"})
            body = json.loads(request.content)
            self._next_id += 1
            row = {
                **body,
                "id": self._next_id,
                "status": "active",
                "created_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=30)).isoformat(),
            }
            self.submitted.append(row)
            self.notes.insert(0, row)
            return httpx.Response(200, json=row)
        if path == "/research/view" and request.method == "GET":
            if key != CAPTURE and key != OPERATOR:
                return httpx.Response(401, json={"detail": "unauthorized"})
            return httpx.Response(200, content=json.dumps(self.view))  # null when none
        if path == "/usage":
            if key != OPERATOR:
                return httpx.Response(403, json={"scope": "capture"})
            return httpx.Response(200, json={"rows": [], "totals": {"cost_usd": self.usage_total}})
        return httpx.Response(404, json={"detail": path})

    def http(self, key: str = CAPTURE) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            headers={"x-api-key": key},
            transport=httpx.MockTransport(self.handler),
        )


def live_note(**overrides) -> dict:
    base = {
        "id": 7,
        "title": "Spot ETH ETFs saw $120m net outflows on Sep 28",
        "body": "…",
        "tags": ["grok-desk", "crypto-flows"],
        "asset_classes": ["crypto"],
        "symbols": ["ETH/USD"],
        "source": WEB_URL,
        "status": "active",
        "pinned": False,
        "created_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=29)).isoformat(),
        "authored_at": None,
    }
    base.update(overrides)
    return base


def make_runner(
    xai: SeatXaiClient,
    api: FakeApi,
    tmp_path: Path,
    *,
    max_notes: int = 3,
    max_chars: int = 1400,
) -> SeatRunner:
    return SeatRunner(
        CRYPTO_FLOWS,
        xai=xai,
        capture=CaptureClient(api.http()),
        max_notes=max_notes,
        max_chars=max_chars,
        state_path=tmp_path / "seat-runs.jsonl",
        now=NOW,
    )


# --- seat definition -----------------------------------------------------------


def test_crypto_flows_seat_is_registered_with_the_design_scope() -> None:
    assert SEATS["crypto-flows"] is CRYPTO_FLOWS
    assert set(CRYPTO_FLOWS.symbols_allowed) == {"ETH/USD", "BTC/USD"}
    assert CRYPTO_FLOWS.asset_classes == ("crypto",)
    assert CRYPTO_FLOWS.tags == ("grok-desk", "crypto-flows")
    for topic in ("ETF", "stablecoin", "funding", "on-chain", "exchange"):
        assert topic.lower() in CRYPTO_FLOWS.user_prompt.lower()


# --- response parsing ------------------------------------------------------------


def test_parse_responses_payload_collects_text_citations_usage_and_tool_calls() -> None:
    payload = responses_payload(
        [candidate()],
        [WEB_URL, X_URL],
        usage=(1234, 56),
        tool_items=[
            {"type": "x_search_call"},
            {"type": "web_search_call"},
            {"type": "web_search_call"},
        ],
    )
    result = parse_responses_payload(payload)
    assert '"notes"' in result.text
    assert result.citations == [WEB_URL, X_URL]  # annotation + top-level, deduped
    assert result.usage == Usage(prompt_tokens=1234, completion_tokens=56)
    assert result.x_search_calls == 1
    assert result.web_search_calls == 2


def test_parse_responses_payload_infers_one_call_per_tool_from_citations() -> None:
    result = parse_responses_payload(responses_payload([], [WEB_URL, X_URL]))
    assert result.x_search_calls == 1
    assert result.web_search_calls == 1
    empty = parse_responses_payload(responses_payload([], []))
    assert (empty.x_search_calls, empty.web_search_calls) == (0, 0)


def test_parse_notes_tolerates_prose_around_the_json_and_caps_nothing() -> None:
    text = "Here you go:\n" + json.dumps({"notes": [candidate(), candidate(title="two")]}) + "\nbye"
    notes = parse_notes(text)
    assert [n.title for n in notes] == [candidate()["title"], "two"]
    assert notes[0].citations == [WEB_URL, X_URL]


def test_parse_notes_rejects_garbage_and_accepts_an_empty_sweep() -> None:
    with pytest.raises(seat_runner.SeatError):
        parse_notes("no json here")
    assert parse_notes('{"notes": []}') == []


def test_candidate_note_normalizes_dates_symbols_and_confidence() -> None:
    note = CandidateNote.model_validate(
        candidate(authored_at="2099-01-01T00:00:00+00:00", symbols=["ETH/USD", "DOGE/USD"])
    )
    assert note.authored_at_utc(now=NOW) is None  # future → omitted, never guessed
    naive = CandidateNote.model_validate(candidate(authored_at="2026-09-28T22:00:00"))
    assert naive.authored_at_utc(now=NOW) is None  # naive → omitted
    ok = CandidateNote.model_validate(candidate())
    assert ok.authored_at_utc(now=NOW) == datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
    assert note.scoped_symbols(CRYPTO_FLOWS) == ["ETH/USD"]
    with pytest.raises(ValidationError):
        CandidateNote.model_validate(candidate(confidence=1.5))


# --- gates -----------------------------------------------------------------------


def test_grounding_gate_drops_notes_without_a_returned_citation() -> None:
    returned = {WEB_URL, X_URL}
    grounded = ground(CandidateNote.model_validate(candidate()), returned)
    assert grounded is not None and grounded.citations == [WEB_URL, X_URL]

    # cites nothing → dropped
    assert ground(CandidateNote.model_validate(candidate(citations=[])), returned) is None
    # cites only URLs the tool layer never returned (model prior) → dropped
    ghost = CandidateNote.model_validate(candidate(citations=["https://example.com/made-up"]))
    assert ground(ghost, returned) is None
    # a mix keeps the grounded subset and repoints source at a grounded URL
    mixed = CandidateNote.model_validate(
        candidate(citations=["https://example.com/made-up", X_URL], source="https://example.com/x")
    )
    kept = ground(mixed, returned)
    assert kept is not None and kept.citations == [X_URL] and kept.source == X_URL
    # nothing returned at all → nothing survives
    assert ground(CandidateNote.model_validate(candidate()), set()) is None


def test_dedupe_gate_matches_title_or_source_within_seven_days() -> None:
    note = CandidateNote.model_validate(candidate())
    assert is_duplicate(note, [live_note()], now=NOW)
    # same source, different title
    assert is_duplicate(note, [live_note(title="different")], now=NOW)
    # same title modulo case/whitespace, different source
    assert is_duplicate(
        note,
        [live_note(title="  spot eth etfs saw $120M net outflows on sep 28 ", source=None)],
        now=NOW,
    )
    # trailing slash / scheme noise on the source still matches
    assert is_duplicate(
        note, [live_note(title="x", source="http://coindesk.com/etf-flows/")], now=NOW
    )
    # a match older than the window is not a duplicate (a repeated claim is new information)
    old = live_note(created_at=(NOW - timedelta(days=8)).isoformat())
    assert not is_duplicate(note, [old], now=NOW)
    # unrelated
    assert not is_duplicate(note, [live_note(title="Rates", source=OTHER_URL)], now=NOW)


# --- cost accounting -------------------------------------------------------------


def test_estimate_cost_prices_tokens_x_search_units_and_web_search_calls() -> None:
    usage = Usage(prompt_tokens=100_000, completion_tokens=4_000)
    cost = estimate_cost(
        "grok-4.3",
        usage,
        citations=[X_URL, "https://x.com/farside", WEB_URL],
        x_search_calls=2,
        web_search_calls=3,
        when=NOW,  # after the 2026-09-21 per-post regime
    )
    assert isinstance(cost, SweepCost)
    assert cost.input_tokens == 100_000 and cost.output_tokens == 4_000
    assert cost.token_usd == pytest.approx((100_000 * 1.25 + 4_000 * 2.50) / 1_000_000)
    assert cost.x_search_posts == 1 and cost.x_search_profiles == 1
    assert cost.x_search_usd == pytest.approx((1 * 5.0 + 1 * 10.0) / 1000)
    assert cost.web_search_usd == pytest.approx(3 * 5.0 / 1000)
    assert cost.total_usd == pytest.approx(cost.token_usd + cost.x_search_usd + cost.web_search_usd)
    assert cost.to_dict()["total_usd"] == pytest.approx(cost.total_usd)


def test_sweep_costs_add() -> None:
    a = estimate_cost(
        "grok-4.3", Usage(10, 10), citations=[], x_search_calls=0, web_search_calls=1, when=NOW
    )
    b = estimate_cost(
        "grok-4.3", Usage(20, 5), citations=[X_URL], x_search_calls=1, web_search_calls=0, when=NOW
    )
    total = a + b
    assert total.input_tokens == 30 and total.output_tokens == 15
    assert total.web_search_calls == 1 and total.x_search_calls == 1
    assert total.total_usd == pytest.approx(a.total_usd + b.total_usd)


# --- the xAI request shape ---------------------------------------------------------


async def test_sweep_request_uses_responses_with_both_tools_and_a_one_day_window() -> None:
    captured: list[dict] = []
    xai = xai_client([responses_payload([], [])], captured)
    await xai.sweep(
        CRYPTO_FLOWS,
        from_date="2026-09-28",
        to_date="2026-09-29",
        already_filed=["ETF flows negative"],
        max_notes=3,
        max_chars=1400,
    )
    body = captured[0]
    assert body["model"] == "grok-4.3"
    assert body["store"] is False
    tools = {t["type"]: t for t in body["tools"]}
    assert set(tools) == {"x_search", "web_search"}
    assert tools["x_search"]["from_date"] == "2026-09-28"
    assert tools["x_search"]["to_date"] == "2026-09-29"
    assert body["text"]["format"]["type"] == "json_schema"
    text = json.dumps(body["input"])
    assert "ETF flows negative" in text  # already-filed titles reach the prompt
    assert "1400" in text and "3" in text
    assert "2026-09-28" in text


async def test_distill_request_sends_no_tools() -> None:
    captured: list[dict] = []
    xai = xai_client([responses_payload("shorter body", [])], captured)
    result = await xai.distill("x" * 3000, max_chars=1400)
    assert result.text == "shorter body"
    assert "tools" not in captured[0]
    assert "1400" in json.dumps(captured[0]["input"])


# --- the runner end to end ----------------------------------------------------------


async def test_run_submits_grounded_notes_with_the_capture_key_and_records_the_run(tmp_path: Path):
    api = FakeApi(notes=[live_note(id=3, title="Old unrelated", source=OTHER_URL)])
    captured: list[dict] = []
    xai = xai_client(
        [
            responses_payload(
                [
                    candidate(),
                    candidate(
                        title="BTC funding flipped negative", source=X_URL, symbols=["BTC/USD"]
                    ),
                ],
                [WEB_URL, X_URL],
            )
        ],
        captured,
    )
    runner = make_runner(xai, api, tmp_path)

    summary = await runner.run()

    assert summary["seat"] == "crypto-flows"
    assert summary["dry_run"] is False
    assert [s["title"] for s in summary["submitted"]] == [
        candidate()["title"],
        "BTC funding flipped negative",
    ]
    assert summary["dropped"] == []
    assert summary["cost_usd"]["total_usd"] > 0

    # Write path: POST /research/notes with the capture key only.
    posts = [r for r in api.requests if r.method == "POST"]
    assert len(posts) == 2
    assert all(r.headers["x-api-key"] == CAPTURE for r in api.requests)
    first = api.submitted[0]
    assert first["title"] == candidate()["title"]
    assert first["symbols"] == ["ETH/USD"]
    assert first["asset_classes"] == ["crypto"]
    assert first["tags"][:2] == ["grok-desk", "crypto-flows"] and "etf" in first["tags"]
    assert first["source"] == WEB_URL
    assert first["authored_at"] == "2026-09-28T22:00:00+00:00"
    assert "pinned" not in first or first["pinned"] is False
    # The seat never calls anything but the capture surface.
    assert {r.url.path for r in api.requests} <= {"/research/notes"}

    # The already-filed list came from the live notes.
    assert "Old unrelated" in json.dumps(captured[0]["input"])

    # State file: one JSON line with ids, cost and usage.
    runs = read_runs(tmp_path / "seat-runs.jsonl")
    assert len(runs) == 1
    record = runs[0]
    assert record["seat"] == "crypto-flows"
    assert [s["id"] for s in record["submitted"]] == [101, 102]
    assert record["cost_usd"]["total_usd"] == pytest.approx(summary["cost_usd"]["total_usd"])
    assert record["usage"]["input_tokens"] == 1000
    assert record["window"]["from"] == "2026-09-28" and record["window"]["to"] == "2026-09-29"


async def test_run_drops_ungrounded_duplicate_and_offscope_notes(tmp_path: Path):
    api = FakeApi(notes=[live_note()])  # the ETF note is already live (1 day old)
    xai = xai_client(
        [
            responses_payload(
                [
                    candidate(),  # duplicate of the live note (title + source)
                    candidate(title="Model prior", citations=[], source=None),  # ungrounded
                    candidate(title="Ghost", citations=["https://example.com/nope"]),  # ghost
                    candidate(
                        title="Stablecoin supply +$2b",
                        source=OTHER_URL,
                        citations=[OTHER_URL],
                        symbols=["DOGE/USD"],
                    ),
                ],
                [WEB_URL, X_URL, OTHER_URL],
            )
        ],
        [],
    )
    runner = make_runner(xai, api, tmp_path)
    summary = await runner.run()

    assert [s["title"] for s in summary["submitted"]] == ["Stablecoin supply +$2b"]
    reasons = {d["title"]: d["reason"] for d in summary["dropped"]}
    assert reasons[candidate()["title"]] == "duplicate"
    assert reasons["Model prior"] == "ungrounded"
    assert reasons["Ghost"] == "ungrounded"
    # off-scope symbol is stripped, not fatal: class-scoped note goes through
    assert api.submitted[0]["symbols"] == [] and api.submitted[0]["asset_classes"] == ["crypto"]


async def test_run_dedupes_within_the_batch_and_caps_at_max_notes(tmp_path: Path):
    api = FakeApi()
    notes = [
        candidate(
            title=f"Note {i}",
            source=f"https://example.com/{i}",
            citations=[f"https://example.com/{i}"],
        )
        for i in range(5)
    ]
    notes.insert(
        1,
        candidate(
            title="Note 0", source="https://example.com/dup", citations=["https://example.com/0"]
        ),
    )
    xai = xai_client([responses_payload(notes, [f"https://example.com/{i}" for i in range(5)])], [])
    runner = make_runner(xai, api, tmp_path, max_notes=3)
    summary = await runner.run()
    assert [s["title"] for s in summary["submitted"]] == ["Note 0", "Note 1", "Note 2"]
    reasons = [d["reason"] for d in summary["dropped"]]
    assert reasons.count("duplicate") == 1
    assert reasons.count("max_notes") == 2


async def test_run_with_zero_citations_submits_nothing_and_still_records_cost(tmp_path: Path):
    api = FakeApi()
    xai = xai_client([responses_payload([candidate()], [])], [])
    summary = await make_runner(xai, api, tmp_path).run()
    assert summary["submitted"] == []
    assert summary["dropped"][0]["reason"] == "ungrounded"
    assert summary["cost_usd"]["total_usd"] > 0
    assert not [r for r in api.requests if r.method == "POST"]
    assert len(read_runs(tmp_path / "seat-runs.jsonl")) == 1


async def test_size_gate_distills_once_then_drops(tmp_path: Path):
    api = FakeApi()
    long_body = "flows " * 400  # ~2400 chars
    captured: list[dict] = []
    xai = xai_client(
        [
            responses_payload(
                [
                    candidate(body=long_body),
                    candidate(title="Also long", body=long_body, source=X_URL),
                ],
                [WEB_URL, X_URL],
            ),
            responses_payload("tight distillation under the cap", []),  # first distill: ok
            responses_payload("y" * 1500, []),  # second distill: still over → dropped
        ],
        captured,
    )
    summary = await make_runner(xai, api, tmp_path).run()
    assert len(captured) == 3
    assert "tools" not in captured[1] and "tools" not in captured[2]
    assert [s["title"] for s in summary["submitted"]] == [candidate()["title"]]
    assert api.submitted[0]["body"] == "tight distillation under the cap"
    assert summary["dropped"] == [{"title": "Also long", "reason": "oversize"}]
    # both distill calls are in the run's cost
    assert summary["usage"]["input_tokens"] == 3000


async def test_dry_run_calls_xai_but_never_posts(tmp_path: Path):
    api = FakeApi()
    xai = xai_client([responses_payload([candidate()], [WEB_URL])], [])
    summary = await make_runner(xai, api, tmp_path).run(dry_run=True)
    assert summary["dry_run"] is True
    assert [s["title"] for s in summary["would_submit"]] == [candidate()["title"]]
    assert summary["submitted"] == []
    assert not [r for r in api.requests if r.method == "POST"]
    assert read_runs(tmp_path / "seat-runs.jsonl")[0]["dry_run"] is True


async def test_capture_client_exposes_only_the_capture_surface() -> None:
    public = {name for name in dir(CaptureClient) if not name.startswith("_")}
    assert public == {"list_notes", "submit_note", "macro_view"}


def test_build_capture_http_uses_the_capture_key_never_the_operator_key() -> None:
    settings = Settings(api_key=OPERATOR, capture_api_key=CAPTURE, api_base_url=API_BASE)
    http = build_capture_http(settings)
    assert http.headers["x-api-key"] == CAPTURE
    assert str(http.base_url).rstrip("/") == API_BASE
    with pytest.raises(seat_runner.SeatError, match="TRADER_CAPTURE_API_KEY"):
        build_capture_http(Settings(api_key=OPERATOR, capture_api_key=""))


def test_build_xai_http_defaults_to_the_api_key_and_refuses_without_one() -> None:
    with pytest.raises(seat_runner.SeatError, match="XAI_API_KEY"):
        build_xai_http(Settings(xai_api_key=""), credential="api_key")
    http = build_xai_http(Settings(xai_api_key="k", xai_base_url=XAI_BASE), credential="api_key")
    assert str(http.base_url).rstrip("/") == XAI_BASE
    with pytest.raises(seat_runner.SeatError, match="TRADER_XAI_TOKEN_KEY"):
        build_xai_http(Settings(xai_api_key=""), credential="oauth")
    with pytest.raises(seat_runner.SeatError, match="credential"):
        build_xai_http(Settings(xai_api_key="k"), credential="bogus")


# --- state file -----------------------------------------------------------------------


def test_state_file_is_append_only_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "seat-runs.jsonl"
    append_run(path, {"seat": "crypto-flows", "ts": "2026-09-28T13:00:00+00:00"})
    append_run(path, {"seat": "crypto-flows", "ts": "2026-09-29T13:00:00+00:00"})
    path.write_text(path.read_text() + "not json\n")  # a corrupt line is skipped, not fatal
    runs = read_runs(path)
    assert [r["ts"][:10] for r in runs] == ["2026-09-29", "2026-09-28"]  # newest first
    assert read_runs(path, limit=1)[0]["ts"].startswith("2026-09-29")
    assert read_runs(tmp_path / "missing.jsonl") == []


# --- CLI --------------------------------------------------------------------------------


def test_cli_seat_run_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from trader.surfaces.cli import main as cli

    api = FakeApi()
    settings = Settings(
        api_key=OPERATOR,
        capture_api_key=CAPTURE,
        xai_api_key="k",
        api_base_url=API_BASE,
        seat_state_path=str(tmp_path / "runs.jsonl"),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(seat_runner, "build_capture_http", lambda s: api.http())

    def fake_xai(s, *, credential):
        assert credential == "api_key"
        assert s.seat_model == "grok-4.3"
        return httpx.AsyncClient(
            base_url=XAI_BASE,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=responses_payload([candidate()], [WEB_URL]))
            ),
        )

    monkeypatch.setattr(seat_runner, "build_xai_http", fake_xai)

    result = CliRunner().invoke(cli.app, ["seat", "run", "crypto-flows", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [s["id"] for s in payload["submitted"]] == [101]
    assert payload["model"] == "grok-4.3"
    assert read_runs(tmp_path / "runs.jsonl")[0]["submitted"][0]["id"] == 101

    result = CliRunner().invoke(cli.app, ["seat", "run", "nope"])
    assert result.exit_code != 0
    assert "crypto-flows" in result.output


def test_cli_seat_run_fails_closed_without_a_capture_key(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from trader.surfaces.cli import main as cli

    settings = Settings(
        api_key=OPERATOR,
        capture_api_key="",
        xai_api_key="k",
        seat_state_path=str(tmp_path / "r.jsonl"),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    result = CliRunner().invoke(cli.app, ["seat", "run", "crypto-flows"])
    assert result.exit_code == 1
    assert "TRADER_CAPTURE_API_KEY" in result.output


# --- settings + systemd templates -------------------------------------------------------


def test_seat_settings_defaults() -> None:
    s = Settings()
    assert s.seat_model == "grok-4.3"
    assert s.seat_max_notes == 3
    assert s.seat_note_max_chars == 1400
    assert s.seat_state_path == "~/.local/state/trader/seat-runs.jsonl"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read_string((ROOT / "infra" / "systemd" / name).read_text())
    return parser


def test_seat_timer_and_service_templates_parse() -> None:
    timer = _unit("trader-seat-crypto-flows.timer")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 13:00:00 UTC"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"

    service = _unit("trader-seat-crypto-flows.service")
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["WorkingDirectory"] == "%h/repos/trader"
    assert service["Service"]["ExecStart"].endswith("trader seat run crypto-flows")
