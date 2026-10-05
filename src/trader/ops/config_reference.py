"""Settings metadata and the effective-config snapshot, derived once from `Settings`.

Two readers share this module so they can never drift: `scripts/gen_config_reference.py`
(renders `docs/configuration.md` + `.env.example` from the field metadata) and the
`config` query on the service registry (`GET /config`, `get_config`, `trader config
show`), which pairs the same metadata with the values of the running `Settings`.

Secrets never leave through the snapshot: every `credential` field reports only whether
it is set, and every other string has URL userinfo, secret-looking query parameters and
libpq `password=` pairs masked (a DSN carries its password inline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import AliasChoices, SecretStr
from pydantic_core import to_jsonable_python

from trader.ops.config import CATEGORIES, CREDENTIAL, SECTIONS, Settings

ENV_PREFIX = str(Settings.model_config.get("env_prefix", ""))
MASK = "***"


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
    reference and the snapshot both refuse an undocumented setting.
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


# --- masking ------------------------------------------------------------------

# A query parameter named like a secret: `?sslpassword=...`, `&api_key=...`, `&token=...`.
_SECRET_QUERY = re.compile(
    r"([?&][^=&#]*(?:pass|secret|token|key|auth|sig)[^=&#]*=)[^&#]*", re.IGNORECASE
)
# libpq keyword/value DSN form: `host=db password=hunter2 dbname=trader`.
_KV_PASSWORD = re.compile(r"(\bpassword\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """Hide credentials embedded in a URL-like or DSN string; anything else is unchanged.

    `scheme://user:pass@host` keeps the user and masks the password; a bare
    `scheme://token@host` masks the whole userinfo (it may itself be a token). Query
    parameters named like a secret and libpq `password=` pairs are masked too.
    """
    out = text
    if "://" in out:
        try:
            parts = urlsplit(out)
        except ValueError:
            parts = None
        if parts is not None and parts.netloc:
            netloc = parts.netloc
            if "@" in netloc:
                userinfo, _, hostport = netloc.rpartition("@")
                user, sep, _password = userinfo.partition(":")
                masked = f"{user}:{MASK}" if sep else MASK
                out = out.replace(netloc, f"{masked}@{hostport}", 1)
            out = _SECRET_QUERY.sub(rf"\g<1>{MASK}", out)
    return _KV_PASSWORD.sub(rf"\g<1>{MASK}", out)


def _public(value: Any) -> Any:
    """JSON form of a non-credential value, with any embedded secret masked."""
    if isinstance(value, SecretStr):  # defensive: a SecretStr is never shown, whatever its category
        return None
    jsonable = to_jsonable_python(value)
    return mask_secrets(jsonable) if isinstance(jsonable, str) else jsonable


def _is_set(value: Any) -> bool:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return value not in (None, "")


# --- snapshot -----------------------------------------------------------------


def snapshot(settings: Settings) -> dict[str, Any]:
    """The effective configuration of ``settings``, grouped by section in SECTIONS order.

    Each entry carries the field's metadata, its default and its effective value
    (env + `.env`). Credentials report ``value: null`` and only whether they are set.
    """
    by_section: dict[str, list[dict[str, Any]]] = {section: [] for section in SECTIONS}
    for doc in collect_fields():
        raw = getattr(settings, doc.name)
        secret = doc.category == CREDENTIAL or isinstance(raw, SecretStr)
        if secret:
            value = None
            is_set = _is_set(raw)
            changed = is_set
        else:
            value = _public(raw)
            changed = to_jsonable_python(raw) != to_jsonable_python(doc.default)
            is_set = changed
        by_section[doc.section].append(
            {
                "field": doc.name,
                "env": doc.env_name,
                "env_aliases": list(doc.env_names[1:]),
                "category": doc.category,
                "type": doc.type_name,
                "default": None if secret else _public(doc.default),
                "value": value,
                "set": is_set,
                "changed": changed,
                "description": doc.description,
            }
        )
    return {
        "sections": [
            {"name": section, "settings": settings_list}
            for section, settings_list in by_section.items()
        ]
    }
