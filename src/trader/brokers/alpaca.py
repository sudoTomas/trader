"""Alpaca broker — PAPER ONLY unless explicitly, deliberately enabled.

Raw httpx + websockets rather than ``alpaca-py``: the SDK pulls pandas/requests/msgpack
into a memory-budgeted box (the GPU host shares its memory with vLLM), and the wire
protocol is small and fully documented. API facts below are from a primary-source
spike done for v1 (its ``docs/research/alpaca-api/findings.md``, researched
2026-08-05); each non-obvious one is called out where it bites.

Safety: ``AlpacaBroker`` REFUSES to construct against the live URL unless
``TRADER_ALPACA_LIVE_ENABLED=true``. The default build cannot reach live at all.

Venue facts that shape this code:

- Auth headers ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``; paper keys are distinct
  credentials from live.
- ``client_order_id`` is the idempotency key (max 128 chars), enforced unique against
  *active* orders → a duplicate returns HTTP 422. That 422 is how we learn a previous
  submit actually landed, so it maps to ``DuplicateOrder``, never to a resubmit.
- **Crypto cannot use ``notional``**: notional requires ``market`` + TIF ``day``, but
  crypto only supports TIF ``gtc``/``ioc``. Notional crypto intents are converted to
  ``qty`` by the caller (the executor) using the price it already has.
- Crypto is long-only at the venue (matching the RiskGuard class policy).
- The trade-updates stream sends **binary** frames and offers **no replay**: events during
  a disconnect are lost, so every (re)connect must be followed by a REST reconcile.
- All numeric fields come back as strings.
- REST budget is 200 req/min per account.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
import websockets

from trader.brokers.base import (
    AccountSnapshot,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    DuplicateOrder,
    OrderEvent,
)
from trader.ops.config import Settings

log = structlog.get_logger(__name__)

PAPER_REST = "https://paper-api.alpaca.markets"
PAPER_WS = "wss://paper-api.alpaca.markets/stream"
LIVE_REST = "https://api.alpaca.markets"
LIVE_WS = "wss://api.alpaca.markets/stream"

PROVIDER = "alpaca_trading"
_BODY_LIMIT = 200


def _failure_detail(exc: BaseException) -> str:
    """Class, HTTP status, and a truncated body.

    ``str()`` of an httpx timeout or connect error is often empty (httpcore maps
    a bare ``TimeoutError``), which logged as ``alpaca: `` on 2026-09-24.
    """
    parts = [type(exc).__name__]
    response = getattr(exc, "response", None)
    if isinstance(response, httpx.Response):
        parts.append(f"HTTP {response.status_code}")
        body = " ".join(response.text.split())
        if body:
            parts.append(body[:_BODY_LIMIT])
    else:
        message = " ".join(str(exc).split())
        if message:
            parts.append(message[:_BODY_LIMIT])
    return " ".join(parts)


def _f(value: Any, default: float = 0.0) -> float:
    """Alpaca returns numbers as strings; missing/blank means default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AlpacaBroker:
    """Alpaca Trading API client. Paper by default, live only when explicitly enabled."""

    name = "alpaca"

    def __init__(
        self,
        key_id: str,
        secret: str,
        *,
        live: bool = False,
        live_enabled: bool = False,
        client: httpx.AsyncClient | None = None,
        ws_connect=None,
    ) -> None:
        if live and not live_enabled:
            # The prime directive's outermost ring: a live client cannot be built.
            raise BrokerError(
                "refusing to construct a LIVE Alpaca client: set TRADER_ALPACA_LIVE_ENABLED=true "
                "to permit it. This build is paper-only."
            )
        if not (key_id and secret):
            raise BrokerError("AlpacaBroker requires ALPACA_API_KEY_ID and ALPACA_API_SECRET")

        self.live = live
        self._key_id = key_id
        self._secret = secret
        self._ws_url = LIVE_WS if live else PAPER_WS
        self._ws_connect = ws_connect or websockets.connect
        self._client = client or httpx.AsyncClient(
            base_url=LIVE_REST if live else PAPER_REST,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=15.0,
        )
        log.info("alpaca_broker_ready", live=live)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> AlpacaBroker:
        return cls(
            settings.alpaca_api_key_id,
            settings.alpaca_api_secret,
            live=settings.alpaca_live_enabled,
            live_enabled=settings.alpaca_live_enabled,
            **kwargs,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- REST --------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"alpaca: {_failure_detail(exc)}") from exc
        if resp.status_code == 422 and "client_order_id" in resp.text:
            raise DuplicateOrder(resp.text[:200])
        if resp.status_code >= 400:
            err = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            raise BrokerError(f"alpaca: {_failure_detail(err)}") from err
        return None if resp.status_code == 204 or not resp.content else resp.json()

    async def is_healthy(self) -> bool:
        try:
            return (await self.get_account()).healthy
        except BrokerError:
            return False

    async def get_account(self) -> AccountSnapshot:
        data = await self._request("GET", "/v2/account")
        blocked = (
            data.get("trading_blocked")
            or data.get("account_blocked")
            or data.get("trade_suspended_by_user")
        )
        return AccountSnapshot(
            equity=_f(data.get("equity")),
            cash=_f(data.get("cash")),
            buying_power=_f(data.get("buying_power")),
            non_marginable_buying_power=_f(data.get("non_marginable_buying_power")),
            status=data.get("status", "UNKNOWN"),
            tradable=data.get("status") == "ACTIVE" and not blocked,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self._request("GET", "/v2/positions") or []
        # Venue spellings (ETHUSD) map back to canonical tickers (ETH/USD) so reconcile
        # and adopt compare like with like — raw-string comparison saw phantom double
        # drift on the first crypto fill (2026-09-14). Unknown spellings pass through
        # raw: a position we never registered should still read as drift, honestly.
        from trader.core.symbols import default_registry

        to_canonical = {
            s.for_provider("alpaca_trading"): s.ticker for s in default_registry().all()
        }
        return [
            BrokerPosition(
                symbol=to_canonical.get(row["symbol"], row["symbol"]),
                qty=_f(row.get("qty")),
                # qty_available excludes shares already committed to open orders — using
                # qty for a close would oversell (see docs/lessons.md#closes-must-land).
                qty_available=_f(row.get("qty_available"), _f(row.get("qty"))),
                avg_entry_price=_f(row.get("avg_entry_price")) or None,
                asset_class=row.get("asset_class"),
            )
            for row in rows
        ]

    def _to_order(self, data: dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            client_order_id=data.get("client_order_id", ""),
            broker_order_id=data.get("id", ""),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            status=data.get("status", ""),
            filled_qty=_f(data.get("filled_qty")),
            filled_avg_price=_f(data.get("filled_avg_price")) or None,
            raw=data,
        )

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Point lookup by idempotency key — the recovery path after an ambiguous submit."""
        try:
            data = await self._request(
                "GET",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except BrokerError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return self._to_order(data) if data else None

    async def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        time_in_force: str = "gtc",
        limit_price: float | None = None,
    ) -> BrokerOrder:
        if (qty is None) == (notional is None):
            raise BrokerError("submit_order needs exactly one of qty or notional")
        if len(client_order_id) > 128:
            raise BrokerError("client_order_id exceeds Alpaca's 128-character limit")
        body: dict[str, Any] = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if qty is not None:
            body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")  # venue accepts 9 dp
        else:
            body["notional"] = round(notional, 2)
        if limit_price is not None:
            body["limit_price"] = limit_price
        data = await self._request("POST", "/v2/orders", json=body)
        return self._to_order(data)

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._request("DELETE", f"/v2/orders/{broker_order_id}")

    async def close_position(self, symbol: str) -> BrokerOrder | None:
        """Venue-side flatten for one symbol (used by flatten_all)."""
        data = await self._request("DELETE", f"/v2/positions/{symbol}")
        return self._to_order(data) if data else None

    async def close_all_positions(self, cancel_orders: bool = True) -> list[BrokerOrder]:
        data = await self._request(
            "DELETE", "/v2/positions", params={"cancel_orders": str(cancel_orders).lower()}
        )
        return [self._to_order(row.get("body", row)) for row in (data or [])]

    # -- trade-updates stream ---------------------------------------------

    async def stream_trade_updates(self) -> AsyncIterator[OrderEvent]:
        """Yield order events until cancelled.

        The stream has **no replay**: anything emitted while disconnected is gone, so the
        caller must REST-reconcile after every (re)connect. Frames are binary JSON.
        """
        async with self._ws_connect(self._ws_url) as ws:
            await ws.send(
                json.dumps({"action": "auth", "key": self._key_id, "secret": self._secret})
            )
            auth_reply = _decode(await ws.recv())
            status = (auth_reply.get("data") or {}).get("status")
            if status != "authorized":
                raise BrokerError(f"alpaca stream auth failed: {status or auth_reply}")

            await ws.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            log.info("alpaca_stream_listening")

            async for frame in ws:
                message = _decode(frame)
                if message.get("stream") != "trade_updates":
                    continue
                event = _to_event(message.get("data") or {})
                if event is not None:
                    yield event


def _decode(frame: str | bytes) -> dict[str, Any]:
    """Paper /stream sends binary frames — decode bytes before parsing."""
    if isinstance(frame, bytes | bytearray):
        frame = frame.decode("utf-8")
    with suppress(json.JSONDecodeError):
        parsed = json.loads(frame)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
    return {}


def _to_event(data: dict[str, Any]) -> OrderEvent | None:
    order = data.get("order") or {}
    if not order:
        return None
    raw_ts = data.get("timestamp") or order.get("updated_at")
    try:
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        ts = datetime.now(tz=UTC)
    return OrderEvent(
        event=data.get("event", ""),
        client_order_id=order.get("client_order_id", ""),
        broker_order_id=order.get("id", ""),
        status=order.get("status", ""),
        ts=ts,
        filled_qty=_f(data.get("qty")) or _f(order.get("filled_qty")),
        fill_price=_f(data.get("price")) or _f(order.get("filled_avg_price")) or None,
        execution_id=data.get("execution_id"),
        # Venue-authoritative resulting position. Crypto fees are deducted FROM THE
        # ASSET, so gross fill qty overstates the position by the fee (~25 bps).
        position_qty=_f(data.get("position_qty")) if data.get("position_qty") else None,
        raw=data,
    )
