"""Read-only Telegram channel reader: sealing the login, mapping messages, and building
the reader from the stored session. No Telegram: messages are SimpleNamespace fakes."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from trader.calls import reader as reader_mod
from trader.calls.reader import RawPost, TelethonReader, reader_from_store, seal, unseal
from trader.ops.config import Settings
from trader.store.repos import ChannelSessionsRepo

KEY = Fernet.generate_key().decode()
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_seal_round_trips_and_hides_plaintext() -> None:
    token = seal(KEY, "1BVtsOK8Bu-session")
    assert token != "1BVtsOK8Bu-session"
    assert "1BVtsOK8Bu-session" not in token
    assert unseal(KEY, token) == "1BVtsOK8Bu-session"


def test_seal_refuses_without_a_key() -> None:
    with pytest.raises(ValueError, match="TRADER_SECRETS_KEY"):
        seal("", "x")


def test_unseal_with_the_wrong_key_says_so() -> None:
    token = seal(KEY, "x")
    with pytest.raises(ValueError, match="does not decrypt"):
        unseal(Fernet.generate_key().decode(), token)


def test_message_to_rawpost_maps_fields() -> None:
    class MessageMediaPhoto:
        pass

    edited = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    msg = SimpleNamespace(
        id=42,
        date=T0,
        edit_date=edited,
        reply_to_msg_id=40,
        message="BTC long 70000",
        media=MessageMediaPhoto(),
    )
    assert reader_mod._to_rawpost(msg) == RawPost(
        msg_id=42,
        posted_at=T0,
        edited_at=edited,
        reply_to=40,
        text="BTC long 70000",
        media="MessageMediaPhoto",
    )


def test_message_to_rawpost_defaults_empty_text_and_no_media() -> None:
    msg = SimpleNamespace(
        id=7, date=T0, edit_date=None, reply_to_msg_id=None, message=None, media=None
    )
    post = reader_mod._to_rawpost(msg)
    assert post.text == ""
    assert post.media is None
    assert post.edited_at is None
    assert post.reply_to is None


def test_reader_never_calls_send_or_read_ack() -> None:
    source = inspect.getsource(reader_mod)
    assert "send_" not in source
    assert "read_acknowledge" not in source


async def test_reader_from_store_is_none_without_a_session(db) -> None:
    assert await reader_from_store(db, Settings(secrets_key=KEY)) is None


async def test_reader_from_store_is_none_without_a_key(db) -> None:
    await ChannelSessionsRepo(db).put(
        "spark", api_id=1, api_hash_enc=seal(KEY, "h"), session_enc=seal(KEY, "s")
    )
    assert await reader_from_store(db, Settings(secrets_key="")) is None


async def test_reader_from_store_unseals_the_stored_session(db) -> None:
    await ChannelSessionsRepo(db).put(
        "desk", api_id=12345, api_hash_enc=seal(KEY, "hash"), session_enc=seal(KEY, "sess")
    )
    reader = await reader_from_store(db, Settings(secrets_key=KEY, calls_session_name="desk"))
    assert isinstance(reader, TelethonReader)
    assert (reader._api_id, reader._api_hash, reader._session) == (12345, "hash", "sess")
