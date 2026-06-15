"""Telethon client factory (userbot / MTProto client API).

The system logs in as a real Telegram account using a pre-generated
``StringSession`` (see scripts/generate_session.py). Telegram is only the UI;
the database remains the single source of truth.
"""
from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from ..config import settings


def build_client() -> TelegramClient:
    client = TelegramClient(
        StringSession(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
        # Keep behaviour deterministic and resilient for an unattended userbot.
        auto_reconnect=True,
        retry_delay=5,
        connection_retries=None,  # retry forever
        # Auto-sleep FloodWaits up to this many seconds instead of raising them,
        # so a transient rate-limit never crashes a worker or the command handler.
        flood_sleep_threshold=settings.flood_sleep_threshold,
    )
    return client
