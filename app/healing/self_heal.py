"""Self-healing.

Periodically (and on demand via ``.repair``) the system repairs itself:

  * pending (unclassified) events are pushed back through the pipeline so a now
    richer thread context can resolve them; events that keep failing past
    ``PENDING_MAX_ATTEMPTS`` are dropped;
  * media without resolved metadata are re-queried against the providers;
  * media whose card is marked dirty are re-rendered.

Reprocessing reuses the normal pipeline (it re-enqueues by event id), so there
is exactly one detection/merge code path and no duplicated logic.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import EventStage, Media, MediaType
from ..storage.repositories import (
    EventRepository,
    MediaRepository,
    PendingRepository,
)
from ..providers.registry import ProviderRegistry
from ..pipeline.queues import ingest_queue, update_queue

log = get_logger("healing.self_heal")

_registry: Optional[ProviderRegistry] = None


def init_healing(registry: ProviderRegistry) -> None:
    """Wire the provider registry so on-demand .repair can resolve metadata."""
    global _registry
    _registry = registry


async def run_healing_cycle() -> dict:
    summary = {
        "pending_retried": 0,
        "pending_dropped": 0,
        "reresolved": 0,
        "rerendered": 0,
    }
    await _retry_pending(summary)
    await _reresolve_metadata(summary)
    await _rerender_dirty(summary)
    log.info("Healing cycle: %s", summary)
    return summary


async def _retry_pending(summary: dict) -> None:
    pending = await PendingRepository.list()
    for item in pending:
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        attempts = item.get("attempts", 0)
        if attempts >= settings.pending_max_attempts:
            await PendingRepository.remove(chat_id, message_id)
            summary["pending_dropped"] += 1
            continue
        await PendingRepository.increment_attempt(chat_id, message_id)
        event_id = item.get("event_id")
        if not event_id:
            continue
        # Reset the event and let the normal pipeline re-evaluate it. If it is
        # still unresolved it will simply be re-added to pending_events.
        await EventRepository.set_stage(event_id, EventStage.INGESTED.value)
        await PendingRepository.remove(chat_id, message_id)
        await ingest_queue.put(event_id)
        summary["pending_retried"] += 1


async def _reresolve_metadata(summary: dict) -> None:
    if _registry is None:
        return
    unresolved = await MediaRepository.find_unresolved()
    for media in unresolved:
        # Try the stored title AND every alternative query candidate (file name /
        # caption / post text) so an entry whose file title never matched can be
        # resolved from the post-text title on a later .repair.
        queries: list[str] = []
        for q in [media.title, *getattr(media, "search_aliases", [])]:
            q = (q or "").strip()
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        if not queries:
            continue
        result = None
        for q in queries:
            r = await _registry.resolve(q, media.media_type, media.year)
            if result is None and r.found:
                result = r
            if r.matched:
                result = r
                break
        if result is None or not result.found or result.metadata is None:
            continue
        meta = result.metadata
        patch = Media(
            media_type=media.media_type,
            title=meta.title or media.title,
            year=meta.year or media.year,
            original_title=meta.original_title,
            overview=meta.overview,
            genres=list(meta.genres),
            rating=meta.rating,
            votes=meta.votes,
            release_date=meta.release_date,
            runtime=meta.runtime,
            poster_url=meta.poster_url,
            authors=list(getattr(meta, "authors", []) or []),
            narrator=getattr(meta, "narrator", "") or "",
            providers={meta.provider: meta.external_id} if meta.external_id else {},
            provider_used=result.provider,
            metadata_resolved=result.matched,
        )
        # Same canonical key -> merge fills the gaps and (because it is freshly
        # resolved) prefers the new metadata.
        patch.canonical_key = media.canonical_key
        await MediaRepository.upsert_merge(patch)
        if result.matched:
            await MediaRepository.set_metadata_resolved(media._id, True)
        await update_queue.put(media._id)
        summary["reresolved"] += 1


async def _rerender_dirty(summary: dict) -> None:
    dirty = await MediaRepository.find_dirty()
    for media in dirty:
        await update_queue.put(media._id)
        summary["rerendered"] += 1


async def healer_loop() -> None:  # pragma: no cover - long-running
    interval = settings.heal_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await run_healing_cycle()
        except Exception as exc:
            log.exception("Healing cycle failed: %s", exc)


async def reverify_audiobooks(enqueue=None) -> dict:
    """Re-check EVERY audiobook entry (resolved or not) with the current precise
    matching. A wrong match (right author, wrong book) is replaced with the
    correct one if it can now be resolved, or cleared back to unresolved so the
    target post stops showing the wrong book. Entries without stored query
    aliases are left untouched (cannot be re-queried reliably).

    ``enqueue`` is an async callable (e.g. ``update_queue.put``) awaited per
    changed media so a bounded queue applies backpressure instead of raising."""
    summary = {"checked": 0, "updated": 0, "cleared": 0, "kept": 0}
    if _registry is None:
        return summary
    ids = await MediaRepository.all_ids()
    for media_id in ids:
        media = await MediaRepository.get(media_id)
        if media is None or media.media_type != MediaType.AUDIOBOOK:
            continue
        summary["checked"] += 1
        queries = [q for q in (getattr(media, "search_aliases", []) or []) if q]
        if not queries:
            summary["kept"] += 1
            continue

        result = None
        for q in queries:
            r = await _registry.resolve(q, MediaType.AUDIOBOOK, media.year)
            if r.matched:
                result = r
                break

        if result is not None and result.metadata is not None:
            meta = result.metadata
            await MediaRepository.apply_metadata(media_id, {
                "title": meta.title or media.title,
                "year": meta.year or media.year,
                "original_title": meta.original_title,
                "overview": meta.overview,
                "genres": list(meta.genres),
                "rating": meta.rating,
                "votes": meta.votes,
                "release_date": meta.release_date,
                "runtime": meta.runtime,
                "poster_url": meta.poster_url,
                "authors": list(getattr(meta, "authors", []) or []),
                "narrator": getattr(meta, "narrator", "") or "",
                "providers": {meta.provider: meta.external_id} if meta.external_id else {},
                "provider_used": meta.provider,
                "metadata_resolved": True,
            })
            summary["updated"] += 1
        elif media.metadata_resolved:
            # Previously "resolved" but no longer matches precisely -> clear the
            # wrong metadata, keep the plain detected title.
            await MediaRepository.apply_metadata(media_id, {
                "title": queries[0],
                "original_title": "", "overview": "", "genres": [],
                "rating": None, "votes": None, "release_date": "", "runtime": None,
                "poster_url": "", "authors": [], "narrator": "",
                "providers": {}, "provider_used": "", "metadata_resolved": False,
            })
            summary["cleared"] += 1
        else:
            summary["kept"] += 1

        if enqueue:
            await enqueue(media_id)
    log.info("Audiobook re-verify: %s", summary)
    return summary


def start_healer(registry: ProviderRegistry) -> asyncio.Task:
    init_healing(registry)
    return asyncio.create_task(healer_loop(), name="healer")
