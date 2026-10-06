"""Settings field metadata, derived once from `Settings`.

`scripts/gen_config_reference.py` renders `docs/configuration.md` and `.env.example` from
it. It lives here, importable, so any other reader of the field metadata (a config query,
a doctor check) uses the same derivation and can never drift from the docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import AliasChoices

from trader.ops.config import CATEGORIES, SECTIONS, Settings

ENV_PREFIX = str(Settings.model_config.get("env_prefix", ""))


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
    """Every `Settings` field with its documentation, in declaration order.

    Raises ValueError when a field lacks its category, section or description: the
    reference refuses an undocumented setting.
    """
    docs: list[FieldDoc] = []
    for name, info in Settings.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        category = str(extra.get("category", ""))
        section = str(extra.get("section", ""))
        if category not in CATEGORIES or section not in SECTIONS:
            raise ValueError(f"{name}: declare it with setting(category=..., section=...)")
        if not info.description:
            raise ValueError(f"{name}: needs a description")
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
