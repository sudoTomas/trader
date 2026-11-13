"""Read-only Telegram channel reader.

It reads with ``iter_messages`` and nothing else: it never posts, reacts or marks a
message seen, so the channel cannot tell it is being read. The login (api_hash and the
session string, a full-account credential) is stored Fernet-sealed with
``TRADER_SECRETS_KEY`` and only unsealed here, to build the client.

Telethon is the optional ``telegram`` extra; it is imported only when a client is built.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from trader.ops.config import Settings
    from trader.store.db import Database


@dataclass(frozen=True)
class RawPost:
    """One channel message as read from Telegram, before it is stored as a version."""

    msg_id: int
    posted_at: datetime
    edited_at: datetime | None
    reply_to: int | None
    text: str
    media: str | None


class ChannelReader(Protocol):
    async def newer(self, channel_id: int, min_id: int, limit: int = 200) -> list[RawPost]:
        """Posts with ``msg_id > min_id``, ascending by msg_id."""
        ...

    async def latest(self, channel_id: int, limit: int) -> list[RawPost]:
        """The newest ``limit`` posts, newest first (for edit checks)."""
        ...

    def history(self, channel_id: int) -> AsyncIterator[RawPost]:
        """Every post, oldest first (an async generator)."""
        ...


def _fernet(secrets_key: str) -> Fernet:
    if not secrets_key:
        raise ValueError("TRADER_SECRETS_KEY is not set, so the Telegram login cannot be sealed")
    try:
        return Fernet(secrets_key.encode())
    except ValueError as exc:
        raise ValueError("TRADER_SECRETS_KEY is not a valid Fernet key") from exc


def seal(secrets_key: str, plaintext: str) -> str:
    """Fernet-encrypt ``plaintext`` with the master key (as ``ProviderDesk._encrypt``)."""
    return _fernet(secrets_key).encrypt(plaintext.encode()).decode()


def unseal(secrets_key: str, token: str) -> str:
    """Decrypt a token from :func:`seal` (as ``ProviderDesk._decrypt``)."""
    try:
        return _fernet(secrets_key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "the stored Telegram login does not decrypt with TRADER_SECRETS_KEY; "
            "was the key changed?"
        ) from exc


def _to_rawpost(message: Any) -> RawPost:
    media = message.media
    return RawPost(
        msg_id=message.id,
        posted_at=message.date,
        edited_at=message.edit_date,
        reply_to=message.reply_to_msg_id,
        text=message.message or "",
        media=type(media).__name__ if media is not None else None,
    )


def _client(api_id: int, api_hash: str, session: str = "") -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session), api_id, api_hash)


class TelethonReader:
    """A :class:`ChannelReader` over a stored user session. Use as ``async with``."""

    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._tg: Any = None

    def __repr__(self) -> str:  # never the hash or the session
        return f"TelethonReader(api_id={self._api_id})"

    async def connect(self) -> None:
        self._tg = _client(self._api_id, self._api_hash, self._session)
        await self._tg.connect()
        if not await self._tg.is_user_authorized():
            await self._tg.disconnect()
            self._tg = None
            raise RuntimeError("the stored Telegram session is not authorized; log in again")

    async def disconnect(self) -> None:
        if self._tg is not None:
            await self._tg.disconnect()
            self._tg = None

    async def __aenter__(self) -> TelethonReader:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def first_name(self) -> str | None:
        me = await self._tg.get_me()
        return getattr(me, "first_name", None)

    async def _entity(self, channel_id: int) -> Any:
        # A fresh StringSession has no entity cache; listing dialogs (a read) fills it.
        try:
            return await self._tg.get_input_entity(channel_id)
        except ValueError:
            await self._tg.get_dialogs()
            return await self._tg.get_input_entity(channel_id)

    async def newer(self, channel_id: int, min_id: int, limit: int = 200) -> list[RawPost]:
        entity = await self._entity(channel_id)
        return [
            _to_rawpost(m)
            async for m in self._tg.iter_messages(entity, min_id=min_id, limit=limit, reverse=True)
        ]

    async def latest(self, channel_id: int, limit: int) -> list[RawPost]:
        entity = await self._entity(channel_id)
        return [_to_rawpost(m) async for m in self._tg.iter_messages(entity, limit=limit)]

    async def history(self, channel_id: int) -> AsyncIterator[RawPost]:
        entity = await self._entity(channel_id)
        async for m in self._tg.iter_messages(entity, reverse=True):
            yield _to_rawpost(m)


async def reader_from_store(db: Database, settings: Settings) -> TelethonReader | None:
    """The reader for ``settings.calls_session_name``, or None when there is no stored
    session or no ``TRADER_SECRETS_KEY`` to unseal it with."""
    from trader.store.repos import ChannelSessionsRepo  # repos imports RawPost from here

    if not settings.secrets_key:
        return None
    row = await ChannelSessionsRepo(db).get(settings.calls_session_name)
    if row is None:
        return None
    key = settings.secrets_key
    return TelethonReader(
        row["api_id"], unseal(key, row["api_hash_enc"]), unseal(key, row["session_enc"])
    )


async def qr_login(
    api_id: int,
    api_hash: str,
    *,
    show_qr: Callable[[str], None],
    password: Callable[[], str],
) -> tuple[str, str | None]:
    """Log in by scanning a QR code from a phone already signed in to Telegram.

    ``show_qr`` renders each login URL (it is recreated when it expires); ``password``
    asks for the 2FA password when the account has one. Returns the session string and
    the account's first name. The caller seals the session; this never prints it.
    """
    from telethon.errors import SessionPasswordNeededError

    client = _client(api_id, api_hash)
    await client.connect()
    try:
        qr = await client.qr_login()
        while True:
            show_qr(qr.url)
            try:
                await qr.wait(timeout=60)
                break
            except TimeoutError:  # asyncio.TimeoutError is TimeoutError on 3.11+
                await qr.recreate()
            except SessionPasswordNeededError:
                await client.sign_in(password=password())
                break
        me = await client.get_me()
        return client.session.save(), getattr(me, "first_name", None)
    finally:
        await client.disconnect()
