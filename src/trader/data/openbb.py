"""OpenBB sidecar client (#141): a thin reader of the ``openbb-api`` REST service.

OpenBB (Apache-2.0) runs as its own process in its own venv (``infra/openbb/``,
``docs/runbooks/deploy.md``); the trader never imports it, only calls its HTTP routes
(``/api/v1/<router>/<command>?provider=...``). Every route answers an OBBject whose
``results`` is a list of records, one record, or a columnar dict of lists (the options
chain); ``fetch`` turns any of them into a list of records. A 204 is OpenBB's "no data".

Only the producer (``trader.research.openbb``) calls this, on its own schedule: nothing
on the decide path waits on the sidecar.
"""

from __future__ import annotations

from typing import Any

import httpx

API_PREFIX = "/api/v1"
#: The earnings calendar fans out one Nasdaq call per day of its window inside OpenBB.
DEFAULT_TIMEOUT_SECONDS = 60.0


class OpenBBError(RuntimeError):
    """One route failed (an HTTP error, or a body that is not an OBBject)."""


class SidecarUnreachable(OpenBBError):
    """The sidecar itself is down: no route answered, so no provider is to blame."""


def records(results: Any) -> list[dict[str, Any]]:
    """An OBBject's ``results`` as a list of records, whatever shape it came in."""
    if results is None:
        return []
    if isinstance(results, list):
        return [r for r in results if isinstance(r, dict)]
    if isinstance(results, dict):
        columns = [v for v in results.values() if isinstance(v, list)]
        if columns and len(columns) == len(results):
            # Columnar (OptionsChainsData): one list per field, all the same length.
            width = min(len(c) for c in columns)
            return [{k: v[i] for k, v in results.items()} for i in range(width)]
        return [results]
    raise OpenBBError(f"unexpected results shape: {type(results).__name__}")


class OpenBBClient:
    """GETs against one ``openbb-api`` base URL."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def from_url(cls, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> OpenBBClient:
        return cls(httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout))

    async def fetch(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """``GET /api/v1/<path>`` with ``params`` (None values dropped) as records."""
        query = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._http.get(f"{API_PREFIX}/{path.lstrip('/')}", params=query)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SidecarUnreachable(f"openbb-api unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OpenBBError(f"{path}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 204:
            return []
        if resp.status_code >= 400:
            raise OpenBBError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OpenBBError(f"{path}: body is not JSON") from exc
        if not isinstance(body, dict):
            raise OpenBBError(f"{path}: body is not an OBBject")
        return records(body.get("results"))

    async def aclose(self) -> None:
        await self._http.aclose()
