"""#27 — the configuration reference is generated from `Settings`, not hand-maintained.

`scripts/gen_config_reference.py` renders every field to `docs/configuration.md` and
`.env.example`; `--check` fails CI when either committed file drifts from the render.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from pydantic import AliasChoices

from trader.ops.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_config_reference.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_config_reference", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load_script()


def _env_names(name: str) -> list[str]:
    info = Settings.model_fields[name]
    alias = info.validation_alias
    if isinstance(alias, AliasChoices):
        return [str(choice) for choice in alias.choices]
    return [f"TRADER_{name.upper()}"]


def test_render_is_deterministic(gen) -> None:
    assert gen.render_markdown() == gen.render_markdown()
    assert gen.render_env_example() == gen.render_env_example()


def test_every_field_is_rendered_in_both_outputs(gen) -> None:
    markdown = gen.render_markdown()
    env_example = gen.render_env_example()
    for name in Settings.model_fields:
        primary, *alternates = _env_names(name)
        assert f"`{primary}`" in markdown, f"{name} missing from docs/configuration.md"
        for alternate in alternates:
            assert f"`{alternate}`" in markdown, f"{name} alternate {alternate} missing"
        assert re.search(rf"^(# )?{re.escape(primary)}=", env_example, re.M), (
            f"{name} missing from .env.example"
        )


def test_every_field_carries_description_and_category(gen) -> None:
    for doc in gen.collect_fields():
        assert doc.description, f"{doc.name} has no description"
        assert doc.category in gen.CATEGORIES, f"{doc.name} category {doc.category!r}"
        assert doc.section in gen.SECTIONS, f"{doc.name} section {doc.section!r}"
    assert {doc.name for doc in gen.collect_fields()} == set(Settings.model_fields)


def test_gates_are_marked_and_default_dark(gen) -> None:
    gates = {doc.name: doc for doc in gen.collect_fields() if doc.category == "gate"}
    for name in (
        "alpaca_live_enabled",
        "shadow_mode",
        "loop_enabled",
        "desk_enabled",
        "briefs_enabled",
        "exit_review_enabled",
        "sentiment_trigger_enabled",
        "auth_disabled",
    ):
        assert name in gates, f"{name} must be documented as a fail-closed gate"
    # A gate's default is the closed position: nothing reaches the venue or the prompt.
    assert gates["alpaca_live_enabled"].default is False
    assert gates["shadow_mode"].default is True
    assert gates["loop_enabled"].default is False


def test_reference_explains_the_nemotron_preset(gen) -> None:
    markdown = gen.render_markdown()
    assert "`TRADER_DECIDE_BASE_URL`" in markdown
    assert "`TRADER_RESEARCH_BASE_URL`" in markdown
    assert "`TRADER_RISK_PARK_ABOVE_USD`" in markdown
    assert "enable_thinking" in markdown and "Nemotron" in markdown


def test_check_passes_on_committed_files(gen) -> None:
    assert gen.check(ROOT) == []
    assert gen.main(["--check"]) == 0


def test_check_fails_on_drift(gen, tmp_path: Path) -> None:
    gen.write(tmp_path)
    assert gen.check(tmp_path) == []
    (tmp_path / ".env.example").write_text("# stale\n")
    drifted = gen.check(tmp_path)
    assert [path.name for path in drifted] == [".env.example"]
    assert gen.main(["--check", "--root", str(tmp_path)]) == 1


def test_env_example_is_safe_by_default(gen) -> None:
    env_example = gen.render_env_example()
    assert "TRADER_ALPACA_LIVE_ENABLED=false" in env_example
    assert "TRADER_AUTH_DISABLED=true" in env_example  # local dev opt-out, documented
    # Secrets ship blank and commented: nothing in the example is a working credential.
    for name in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET", "XAI_API_KEY", "FRED_API_KEY"):
        assert f"# {name}=\n" in env_example
