"""Telegram event handlers.

Two responsibilities:
  1. Capture every real user message in the configured source chats, persist it
     idempotently and push it onto the ingest queue. Bot messages are ignored.
  2. Route owner commands (messages from OWNER_ID starting with the command
     prefix) to the command handler.

Handlers never process media directly; they only enqueue. All heavy work runs
in the pipeline workers.
"""
from __future__ import annotations

from telethon import events

from ..config import settings
from ..logging_setup import get_logger
from ..pipeline.queues import ingest_queue
from ..storage.repositories import EventRepository
from .commands import handle_command
from .parser import parse_message

log = get_logger("telegram.handlers")


def register_handlers(client) -> None:
    source_chats = settings.source_chat_ids
    prefix = settings.command_prefix

    @client.on(events.NewMessage(chats=source_chats or None))
    async def _on_source_message(event):  # pragma: no cover - requires Telegram
        message = event.message
        # Owner commands can be typed inside a source chat too; let the command
        # handler deal with them and do not ingest them as media events.
        if (event.sender_id == settings.owner_id
                and (message.message or "").startswith(prefix)):
            return
        try:
            raw = await parse_message(message)
        except Exception as exc:
            log.warning("Failed to parse message %s: %s", message.id, exc)
            return
        if raw.is_bot:
            return  # ignore other bots
        event_obj, created = await EventRepository.insert_if_new(raw)
        if created:
            await ingest_queue.put(event_obj._id)

    @client.on(events.NewMessage(pattern=None))
    async def _on_command(event):  # pragma: no cover - requires Telegram
        if event.sender_id != settings.owner_id:
            return
        text = event.message.message or ""
        if not text.startswith(prefix):
            return
        try:
            await handle_command(event, text[len(prefix):].strip())
        except Exception as exc:
            log.exception("Command failed: %s", exc)
            try:
                await event.reply(f"\u26a0\ufe0f Command error: {exc}")
            except Exception:
                pass

    log.info("Handlers registered for %d source chat(s).", len(source_chats))
