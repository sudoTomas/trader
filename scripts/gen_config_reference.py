#!/usr/bin/env python
"""Render the configuration reference from `trader.ops.config.Settings` (#27).

Writes `docs/configuration.md` and `.env.example`; `--check` exits 1 when either
committed file differs from the render, so CI catches a setting added without docs.

    uv run python scripts/gen_config_reference.py          # regenerate both files
    uv run python scripts/gen_config_reference.py --check  # CI gate

Field text lives on the `Settings` fields themselves (`setting(...)` in
`src/trader/ops/config.py`); this script only lays it out. `Settings` is never
instantiated here, so the operator's `.env` cannot leak into the output.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import AliasChoices

from trader.ops.config import CATEGORIES, GATE, SECTIONS, Settings

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_PATH = Path("docs") / "configuration.md"
ENV_EXAMPLE_PATH = Path(".env.example")
ENV_PREFIX = str(Settings.model_config.get("env_prefix", ""))

CATEGORY_LEGEND = {
    "gate": (
        "fail-closed safety switch. The default is the closed position; flipping it changes "
        "what can reach the venue or the decide prompt. Flip through the runbook, one at a time."
    ),
    "tuning": "numeric or behavioral parameter; changing it never opens a door by itself.",
    "endpoint": "where a dependency lives: a URL, a served model id, or a path.",
    "credential": "a secret. Blank means that integration stays off; never commit a value.",
}

SECTION_NOTES = {
    "Surfaces / auth": (
        "The API, MCP server and CLI share one service registry; auth guards the first two."
    ),
    "Store": "One Postgres holds everything durable. Market data is never stored (fetch-on-tick).",
    "Safety gates": (
        "Paper only. The LLM proposes, RiskGuard disposes, the broker executes; no setting "
        "bypasses that chain."
    ),
    "Decide loop": "The background scheduler: what it watches, how often, and whether it submits.",
    "Risk limits (RiskGuard)": (
        "Deterministic limits applied to every intent. Code and config only, never prompt-driven; "
        "a change here is a risk-chain behavior change and goes through the decide-change gate."
    ),
    "Research Desk": (
        "Notes, Macro View compilation, briefs, the decision desk and X sentiment. Every flag "
        "here is dark by default and earns its flip through replay, then a shadow window."
    ),
    "LLM endpoints": (
        "Two roles, each any OpenAI-compatible `/v1`: **decide** (also the desk analysts) and "
        "**research** (briefs, compiler, critic, registry sentiment). With "
        "`TRADER_DECIDE_BASE_URL` empty, decide uses the **Nemotron preset** "
        "(`TRADER_NEMOTRON_*`), whose client forces "
        "`chat_template_kwargs: {enable_thinking: false}` on every request; the Spark endpoint "
        "collapses to ~0.35 tok/s without it. A generic URL that equals the Nemotron base URL "
        "gets the same flag; any other URL never receives `chat_template_kwargs`, because vendor "
        "APIs such as OpenAI reject it. Research falls back to xAI when configured, else to the "
        "decide client; `x_search` sentiment is xAI-only."
    ),
    "Broker / market data credentials": (
        "Provider-conventional names without the prefix are the primary spelling; the "
        "`TRADER_`-prefixed alias is accepted too."
    ),
}

# .env.example presentation: which lines ship active (uncommented) and with what value.
# Everything else is a commented `# VAR=default` line the operator uncomments to override.
ENV_EXAMPLE_ACTIVE = {
    "api_key": "",
    "auth_disabled": "true",
    "api_base_url": None,
    "database_url": None,
    "alpaca_live_enabled": None,
    "nemotron_base_url": None,
}
ENV_EXAMPLE_HINTS = {"shadow_symbols": "SPY"}


@dataclass(frozen=True)
class FieldDoc:
    name: str
    env_names: tuple[str, ...]  # primary first, then accepted alternates
    type_name: str
    default: Any
    description: str
    category: str
    section: str

    @property
    def env_name(self) -> str:
        return self.env_names[0]


def _env_names(name: str, alias: Any) -> tuple[str, ...]:
    if isinstance(alias, AliasChoices):
        return tuple(str(choice) for choice in alias.choices)
    if alias is not None:
        return (str(alias),)
    return (f"{ENV_PREFIX}{name.upper()}",)


def collect_fields() -> list[FieldDoc]:
    docs: list[FieldDoc] = []
    for name, info in Settings.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        category = str(extra.get("category", ""))
        section = str(extra.get("section", ""))
        if category not in CATEGORIES or section not in SECTIONS:
            raise SystemExit(f"{name}: declare it with setting(category=..., section=...)")
        if not info.description:
            raise SystemExit(f"{name}: needs a description")
        annotation = info.annotation
        type_name = getattr(annotation, "__name__", str(annotation))
        docs.append(
            FieldDoc(
                name=name,
                env_names=_env_names(name, info.validation_alias),
                type_name=type_name,
                default=info.default,
                description=" ".join(info.description.split()),
                category=category,
                section=section,
            )
        )
    return docs


def env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _md_default(doc: FieldDoc) -> str:
    text = env_value(doc.default)
    return "*(empty)*" if text == "" else f"`{text}`"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown() -> str:
    fields = collect_fields()
    out: list[str] = [
        "# Configuration reference",
        "",
        "<!-- Generated by scripts/gen_config_reference.py from trader.ops.config.Settings. -->",
        "<!-- Do not edit by hand: describe the field in config.py and regenerate. -->",
        "",
        "Every setting is an environment variable read by `trader.ops.config.Settings` "
        "(pydantic-settings, prefix `TRADER_`, names case-insensitive). Values also load from "
        "a `.env` file in the working directory; real environment variables outrank it. "
        "Copy [`.env.example`](../.env.example) to `.env` to start. Regenerate this page and "
        "the example with `uv run python scripts/gen_config_reference.py`; CI runs it with "
        "`--check`.",
        "",
        "## Categories",
        "",
    ]
    for category in CATEGORIES:
        out.append(f"- **{category}** — {CATEGORY_LEGEND[category]}")
    out += [
        "",
        "Gates at a glance (all default closed):",
        "",
    ]
    for doc in fields:
        if doc.category == GATE:
            out.append(f"- `{doc.env_name}` = {_md_default(doc)}")
    out.append("")
    for section in SECTIONS:
        out += [f"## {section}", "", SECTION_NOTES[section], ""]
        out += [
            "| Variable | Type | Default | Category | Description |",
            "|---|---|---|---|---|",
        ]
        for doc in fields:
            if doc.section != section:
                continue
            names = f"`{doc.env_name}`"
            if len(doc.env_names) > 1:
                alternates = ", ".join(f"`{alt}`" for alt in doc.env_names[1:])
                names += f" (alias {alternates})"
            out.append(
                f"| {names} | `{doc.type_name}` | {_md_default(doc)} | {doc.category} "
                f"| {_md_cell(doc.description)} |"
            )
        out.append("")
    return "\n".join(out)


def _wrap_comment(text: str, width: int = 96) -> list[str]:
    lines: list[str] = []
    current = "#"
    for word in text.split():
        if len(current) + 1 + len(word) > width and current != "#":
            lines.append(current)
            current = "#"
        current += " " + word
    lines.append(current)
    return lines


def render_env_example() -> str:
    fields = collect_fields()
    out: list[str] = [
        "# Copy to .env and fill in. Generated by scripts/gen_config_reference.py from",
        "# trader.ops.config.Settings; the full reference is docs/configuration.md.",
        "# Commented lines show the default; uncomment to override. Never commit .env.",
    ]
    for section in SECTIONS:
        out += ["", f"# --- {section} ---"]
        for doc in fields:
            if doc.section != section:
                continue
            out += _wrap_comment(f"[{doc.category}] {doc.description}")
            if len(doc.env_names) > 1:
                out.append(f"# Also accepted: {', '.join(doc.env_names[1:])}")
            if doc.name in ENV_EXAMPLE_ACTIVE:
                override = ENV_EXAMPLE_ACTIVE[doc.name]
                value = env_value(doc.default) if override is None else override
                out.append(f"{doc.env_name}={value}")
            else:
                value = ENV_EXAMPLE_HINTS.get(doc.name, env_value(doc.default))
                out.append(f"# {doc.env_name}={value}")
    out.append("")
    return "\n".join(out)


def outputs() -> dict[Path, str]:
    return {MARKDOWN_PATH: render_markdown(), ENV_EXAMPLE_PATH: render_env_example()}


def write(root: Path) -> list[Path]:
    written = []
    for relative, text in outputs().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def check(root: Path) -> list[Path]:
    """Paths under `root` whose contents differ from the render (missing counts as drift)."""
    drifted = []
    for relative, text in outputs().items():
        path = root / relative
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            drifted.append(path)
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if committed files drift")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)
    if args.check:
        drifted = check(args.root)
        for path in drifted:
            print(f"out of date: {path} (run scripts/gen_config_reference.py)", file=sys.stderr)
        return 1 if drifted else 0
    for path in write(args.root):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
