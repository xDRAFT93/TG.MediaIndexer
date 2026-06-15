"""Owner command system.

Commands (default prefix ``.``):

  .import <chat> [from_msg_id]   Backfill a source chat into the pipeline. With
                                 a message id, only messages after it are read.
                                 Every item is processed under a timeout; on a
                                 hang the item is skipped, marked, and the import
                                 continues with the next one (never blocks).
  .status                        Live counts: media, episodes, pending, posts,
                                 queue sizes.
  .repair                        Run a self-healing cycle now (retry pending,
                                 re-resolve unresolved, re-render dirty cards).
  .rebuild <query>               Force re-resolve + re-render of a media matched
                                 by title.
  .help                          Show this help.

The import only enqueues events; the pipeline workers do the actual detection,
metadata resolution and posting.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from telethon import errors

from ..config import settings
from ..logging_setup import get_logger
from ..pipeline.queues import ingest_queue, processing_queue, update_queue
from ..storage.repositories import (
    EpisodeRepository,
    EventRepository,
    MediaRepository,
    PendingRepository,
    PostRepository,
)
from .parser import parse_message

log = get_logger("telegram.commands")

_STATUS_EVERY = 25


async def handle_command(event, body: str) -> None:
    parts = body.split()
    if not parts:
        await event.reply(_help_text())
        return
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in {"help", "h"}:
        await event.reply(_help_text())
    elif cmd in {"import", "scan"}:
        await _cmd_import(event, args)
    elif cmd in {"status", "stat"}:
        await _cmd_status(event)
    elif cmd in {"repair", "heal"}:
        await _cmd_repair(event)
    elif cmd in {"rebuild", "refresh"}:
        await _cmd_rebuild(event, args)
    else:
        await event.reply(f"Unknown command: {cmd}\n\n{_help_text()}")


def _help_text() -> str:
    p = settings.command_prefix
    return (
        "MediaIndexer commands:\n"
        f"{p}import <chat> [from_msg_id] - backfill a source chat\n"
        f"{p}status - show counts and queue sizes\n"
        f"{p}repair - run a self-healing cycle now\n"
        f"{p}rebuild <query> - re-resolve & re-render a media by title\n"
        f"{p}help - show this help"
    )


async def _resolve_chat(event, token: str):
    if token in {"here", "this", "."}:
        return await event.get_chat()
    # Numeric id (supports -100... supergroup ids) or @username.
    try:
        return await event.client.get_entity(int(token))
    except (ValueError, TypeError):
        return await event.client.get_entity(token)


async def _safe_edit(message, text: str) -> None:
    """Edit a status message without letting a cosmetic update crash the import.

    Telegram rejects an edit whose text is identical to the current message
    (``MessageNotModifiedError``) and rate-limits rapid edits
    (``FloodWaitError``). During a long backfill the status line is edited
    repeatedly, so both are expected and must never abort the import.
    """
    try:
        await message.edit(text)
    except errors.MessageNotModifiedError:
        pass  # text unchanged since the last edit — nothing to do
    except errors.FloodWaitError as exc:
        log.debug("Status edit rate-limited (flood wait %ss); skipped.", exc.seconds)
    except Exception as exc:  # best-effort: a status update must not kill the import
        log.debug("Status edit failed (ignored): %s", exc)


async def _cmd_import(event, args: list[str]) -> None:
    if not args:
        await event.reply("Usage: import <chat_id|@username|here> [from_msg_id]")
        return
    try:
        chat = await _resolve_chat(event, args[0])
    except Exception as exc:
        await event.reply(f"Could not resolve chat {args[0]!r}: {exc}")
        return
    min_id = 0
    if len(args) > 1:
        try:
            min_id = int(args[1])
        except ValueError:
            await event.reply("from_msg_id must be a number.")
            return

    status = await event.reply(f"Import started for {getattr(chat, 'title', args[0])} ...")
    seen = enqueued = skipped = bots = 0
    timeout = settings.item_timeout_seconds

    try:
        # reverse=True iterates oldest -> newest so context builds correctly.
        async for message in event.client.iter_messages(chat, reverse=True, min_id=min_id):
            seen += 1
            try:
                await asyncio.wait_for(
                    _ingest_one(message), timeout=timeout
                )
            except asyncio.TimeoutError:
                skipped += 1
                log.warning("Import: message %s timed out, skipped.", message.id)
            except Exception as exc:
                skipped += 1
                log.warning("Import: message %s failed: %s", message.id, exc)
            else:
                # _ingest_one returns nothing; track counters via side channel.
                pass

            if seen % _STATUS_EVERY == 0:
                await _safe_edit(
                    status,
                    f"Importing {getattr(chat, 'title', args[0])} ...\n"
                    f"seen: {seen} | queued: {ingest_queue.qsize()} pending in queue | "
                    f"skipped: {skipped}",
                )
    except Exception as exc:
        await _safe_edit(status, f"Import aborted after {seen} messages: {exc}")
        return

    await _safe_edit(
        status,
        f"Import finished for {getattr(chat, 'title', args[0])}.\n"
        f"messages read: {seen} | skipped: {skipped}\n"
        f"Events are now being processed by the pipeline (queue: {ingest_queue.qsize()}).",
    )


async def _ingest_one(message) -> None:
    raw = await parse_message(message)
    if raw.is_bot:
        return
    event_obj, created = await EventRepository.insert_if_new(raw)
    if created:
        await ingest_queue.put(event_obj._id)


async def _cmd_status(event) -> None:
    media = await MediaRepository.count()
    pending = await PendingRepository.count()
    posts = await PostRepository.count()
    unresolved = len(await MediaRepository.find_unresolved(limit=10_000))
    dirty = len(await MediaRepository.find_dirty(limit=10_000))
    text = (
        "MediaIndexer status\n"
        f"media: {media} (unresolved: {unresolved}, dirty: {dirty})\n"
        f"pending events: {pending}\n"
        f"ui posts: {posts}\n"
        f"queues - ingest: {ingest_queue.qsize()}, "
        f"processing: {processing_queue.qsize()}, "
        f"update: {update_queue.qsize()}"
    )
    await event.reply(text)


async def _cmd_repair(event) -> None:
    from ..healing.self_heal import run_healing_cycle
    await event.reply("Running self-healing cycle ...")
    summary = await run_healing_cycle()
    await event.reply(
        "Healing done.\n"
        f"pending retried: {summary.get('pending_retried', 0)}\n"
        f"pending resolved: {summary.get('pending_resolved', 0)}\n"
        f"metadata re-resolved: {summary.get('reresolved', 0)}\n"
        f"cards re-rendered: {summary.get('rerendered', 0)}"
    )


async def _cmd_rebuild(event, args: list[str]) -> None:
    if not args:
        await event.reply("Usage: rebuild <title query>")
        return
    query = " ".join(args)
    matches = await MediaRepository.search_text(query)
    if not matches:
        await event.reply(f"No media matched {query!r}.")
        return
    for media in matches:
        await MediaRepository.set_metadata_resolved(media._id, False)
        await MediaRepository.mark_dirty(media._id)
        await update_queue.put(media._id)
    names = ", ".join(f"{m.title} ({m.year or '?'})" for m in matches[:10])
    await event.reply(f"Queued {len(matches)} media for rebuild: {names}")
