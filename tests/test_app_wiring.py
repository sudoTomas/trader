"""``create_app`` wiring: one ``XaiCredentialManager`` per process (#63). Every
builder that reaches api.x.ai — sentiment, desk, compiler, briefs, evaluator, runner,
the probe client — shares the manager built once in ``create_app``, so a single
rotating refresh lineage is spent by exactly one lock (docs/lessons.md#oauth-refresh-margin).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from trader.llm import xai_auth
from trader.llm.xai import ManagedBearerAuth
from trader.ops.config import Settings
from trader.surfaces.api.app import create_app


@pytest.fixture
def count_managers(monkeypatch: pytest.MonkeyPatch) -> dict:
    built = {"n": 0, "instances": []}
    real = xai_auth.XaiCredentialManager

    class Counting(real):
        def __init__(self, *args, **kwargs) -> None:
            built["n"] += 1
            built["instances"].append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(xai_auth, "XaiCredentialManager", Counting)
    return built


def test_create_app_builds_exactly_one_manager(tmp_path: Path, count_managers: dict):
    settings = Settings(
        auth_disabled=True,
        xai_token_key=Fernet.generate_key().decode(),
        xai_token_path=str(tmp_path / "xai_token.enc"),
    )
    app = create_app(settings)
    assert count_managers["n"] == 1
    manager = count_managers["instances"][0]
    assert app.state.xai_manager is manager
    # The probe client (the roles' credential path) resolves its bearer through it.
    xai_http = app.state.xai_http
    assert isinstance(xai_http.auth, ManagedBearerAuth) and xai_http.auth._manager is manager


def test_create_app_builds_no_manager_without_a_token_key(count_managers: dict):
    app = create_app(Settings(auth_disabled=True))
    assert count_managers["n"] == 0
    assert app.state.xai_manager is None
