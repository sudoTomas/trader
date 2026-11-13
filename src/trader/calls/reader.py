"""Read-only Telegram channel reader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawPost:
    """One channel message as read from Telegram, before it is stored as a version."""

    msg_id: int
    posted_at: datetime
    edited_at: datetime | None
    reply_to: int | None
    text: str
    media: str | None
