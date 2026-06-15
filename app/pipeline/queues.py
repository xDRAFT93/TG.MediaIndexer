"""The three pipeline queues and startup resume.

    ingest_queue      -> raw event ids waiting to enter the pipeline
    processing_queue  -> event ids ready for detection / resolution
    update_queue      -> media ids whose card needs (re)rendering

Direct processing is forbidden; everything flows through these queues. The
queues themselves are in-memory, but every item is also durably represented in
MongoDB (event stage / media ui_dirty flag), so a restart can rebuild them via
``resume_pending`` with no loss.
"""
from __future__ import annotations

import asyncio

from ..config import settings
from ..logging_setup import get_logger
from ..storage.repositories import EventRepository, MediaRepository

log = get_logger("pipeline.queues")

ingest_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=settings.queue_maxsize)
processing_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=settings.queue_maxsize)
update_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=settings.queue_maxsize)


async def resume_pending() -> None:
    """Re-enqueue work that was in flight when the process last stopped."""
    resumable = await EventRepository.find_resumable()
    for ev in resumable:
        await ingest_queue.put(ev._id)
    dirty = await MediaRepository.find_dirty()
    for media in dirty:
        await update_queue.put(media._id)
    log.info("Resumed %d event(s) and %d dirty media on startup.",
             len(resumable), len(dirty))
