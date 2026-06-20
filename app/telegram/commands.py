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
  .reindex                       Re-render EVERY catalog entry with the current
                                 display rules (entity limits, footer, links).
  .prune                         Check every entry's source links and remove dead
                                 ones; delete entries whose sources are all gone.
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
    elif cmd in {"reindex", "reapply"}:
        await _cmd_reindex(event)
    elif cmd in {"prune", "cleanup"}:
        await _cmd_prune(event)
    elif cmd in {"tidy", "dropbad"}:
        await _cmd_cleanup(event)
    elif cmd in {"audverify", "fixbooks", "books"}:
        await _cmd_audverify(event)
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
        f"{p}reindex - re-render all entries with current display rules\n"
        f"{p}prune - remove dead source links; delete emptied entries\n"
        f"{p}tidy - delete unresolved entries whose title is just an episode marker\n"
        f"{p}audverify - re-check audiobooks with precise matching; fix/clear wrong ones\n"
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


async def _cmd_reindex(event) -> None:
    """Re-render every catalog entry so old posts pick up the current display
    rules (entity limits, singular 'Quelle', episode links, collapsed quotes).
    Entries whose sources now all live in an ignored thread are removed. This
    re-renders from stored data; it does not re-run detection on past files.
    Use .repair to re-match unresolved entries against the providers.
    """
    from ..healing.prune import _delete_media_fully
    ids = await MediaRepository.all_ids()
    removed = 0
    rerendered = 0
    for media_id in ids:
        media = await MediaRepository.get(media_id)
        if media is None:
            continue
        if _all_sources_ignored(media):
            episodes = await EpisodeRepository.list_for_media(media_id)
            await _delete_media_fully(event.client, media_id, episodes)
            removed += 1
            continue
        if settings.ignore_archive_files and _all_releases_archives(media):
            episodes = await EpisodeRepository.list_for_media(media_id)
            await _delete_media_fully(event.client, media_id, episodes)
            removed += 1
            continue
        await MediaRepository.mark_dirty(media_id)
        await update_queue.put(media_id)
        rerendered += 1
    await event.reply(
        f"Re-indexing {rerendered} entries with the current display rules; "
        f"removed {removed} entry(ies) from ignored threads / archive uploads. "
        f"Run .repair to also re-match unresolved ones."
    )


def _all_releases_archives(media) -> bool:
    """True if the media has film releases and every one is an archive file."""
    from ..pipeline.workers import is_archive_name
    rels = media.releases or []
    names = []
    for r in rels:
        n = r.get("file_name") if isinstance(r, dict) else getattr(r, "file_name", "")
        if n:
            names.append(n)
    return bool(names) and all(is_archive_name(n) for n in names)


def _all_sources_ignored(media) -> bool:
    """True if the media has sources and every one of them is in an ignored
    thread (so it should no longer be catalogued)."""
    if not media.sources or not settings.ignore_thread_ids:
        return False
    tids = set()
    for s in media.sources:
        d = s if isinstance(s, dict) else (s.to_dict() if hasattr(s, "to_dict") else {})
        tids.add(d.get("thread_id"))
    return bool(tids) and all(t in settings.ignore_thread_ids for t in tids)


async def _cmd_prune(event) -> None:
    """Check every entry's source links against Telegram and drop the dead ones;
    delete entries whose sources have all disappeared."""
    from ..healing.prune import run_prune
    await event.reply("Pruning dead source links — checking every entry, this may take a while ...")
    summary = await run_prune(event.client, update_queue.put_nowait)
    await event.reply(
        "Prune done.\n"
        f"entries checked: {summary['checked']}\n"
        f"entries pruned: {summary['media_pruned']}\n"
        f"entries deleted (empty): {summary['media_deleted']}\n"
        f"episodes deleted: {summary['episodes_deleted']}\n"
        f"dead releases removed: {summary['releases_removed']}"
    )


async def _cmd_cleanup(event) -> None:
    """Delete bogus standalone entries whose 'title' is really just an episode
    marker ("1a", "100", "S1F1", "bd2", "10.1" …). These are leftovers from
    before the detection fixes; only unresolved entries with very few releases
    are removed, so real series are never touched."""
    from ..healing.prune import _delete_media_fully
    ids = await MediaRepository.all_ids()
    removed = 0
    for media_id in ids:
        media = await MediaRepository.get(media_id)
        if media is None or media.metadata_resolved:
            continue
        if not _looks_like_episode_marker(media.title):
            continue
        episodes = await EpisodeRepository.list_for_media(media_id)
        if len(episodes) > 3:
            continue  # too substantial to be a stray marker entry
        await _delete_media_fully(event.client, media_id, episodes)
        removed += 1
    await event.reply(
        f"Tidy: removed {removed} unresolved entry(ies) whose title was only an "
        f"episode marker. Run .reindex afterwards to refresh the rest."
    )


async def _cmd_audverify(event) -> None:
    """Re-verify all audiobook entries with the current precise matching, fixing
    or clearing wrong ones so the target thread reflects correct books."""
    from ..healing.self_heal import reverify_audiobooks
    await event.reply("Re-verifying audiobooks with precise matching — checking every book entry ...")
    s = await reverify_audiobooks(update_queue.put_nowait)
    await event.reply(
        "Audiobook re-verify done.\n"
        f"checked: {s['checked']}\n"
        f"corrected/updated: {s['updated']}\n"
        f"cleared (wrong match removed): {s['cleared']}\n"
        f"kept: {s['kept']}"
    )


def _looks_like_episode_marker(title: str) -> bool:
    """True if a stored title is really just an episode marker, not a real title.
    Uses the detector: a marker-only string classifies as episode-only (or yields
    no title at all)."""
    from ..detection.classifier import classify
    t = (title or "").strip()
    if not t:
        return True
    d = classify(t, "", "")
    return d.only_episode or not d.has_title
