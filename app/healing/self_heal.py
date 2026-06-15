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
        query = media.title
        if not query:
            continue
        result = await _registry.resolve(query, media.media_type, media.year)
        if not result.found or result.metadata is None:
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


def start_healer(registry: ProviderRegistry) -> asyncio.Task:
    init_healing(registry)
    return asyncio.create_task(healer_loop(), name="healer")
