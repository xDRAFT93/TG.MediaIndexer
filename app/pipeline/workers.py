"""Pipeline workers.

Three serial workers move every event through the mandated stages:

    ingest_worker      ingest_queue     -> mark PROCESSING -> processing_queue
    processing_worker  processing_queue -> detect, context-match, resolve,
                                           persist (dedup/merge) -> update_queue
    update_worker      update_queue     -> render card -> sync Telegram posts

Each stage is exactly one coroutine so that, within a thread, events are handled
strictly in order. Out-of-order processing would corrupt the active-title
context, so throughput is deliberately traded for correctness. Every item is
wrapped in try/except; a failing item is marked ERROR and the worker continues.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..config import settings
from ..detection import context as ctx
from ..detection.classifier import Detection, classify
from ..detection.patterns import RELEASE_TOKENS, TRAILING_GROUP_RE, VIDEO_EXTENSIONS
from ..logging_setup import get_logger
from ..storage.models import (
    Episode,
    EventStage,
    Media,
    MediaType,
    Release,
    SourceRef,
)
from ..storage.repositories import (
    EpisodeRepository,
    EventRepository,
    MediaRepository,
    PendingRepository,
    ThreadStateRepository,
)
from ..providers.registry import ProviderRegistry, ResolveResult
from .queues import ingest_queue, processing_queue, update_queue

log = get_logger("pipeline.workers")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_release(event) -> Release:
    """Build a Release from the raw event (file_name is the immutable key)."""
    fname = event.file_name or ""
    stem = fname
    if "." in fname:
        base, ext = fname.rsplit(".", 1)
        if ext.lower() in VIDEO_EXTENSIONS:
            stem = base
    tokens = {t for t in _tokenize(stem) if t in RELEASE_TOKENS}
    quality = next((t for t in tokens if t.endswith("p") and t[:-1].isdigit()), "")
    codec = next((t for t in tokens if t in {"x264", "x265", "h264", "h265", "hevc", "av1"}), "")
    source_tag = next((t for t in tokens if t in {
        "bluray", "bdrip", "brrip", "web-dl", "webdl", "webrip", "hdtv", "dvdrip", "remux"
    }), "")
    group = ""
    m = TRAILING_GROUP_RE.search(stem)
    if m:
        group = m.group(0).lstrip("-\u2013")
    return Release(
        file_name=fname,
        quality=quality,
        source_tag=source_tag,
        codec=codec,
        group=group,
        size_bytes=event.size_bytes,
        chat_id=event.chat_id,
        thread_id=event.thread_id,
        message_id=event.message_id,
    )


def _tokenize(name: str) -> list[str]:
    out, cur = [], []
    for ch in name.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


def _source_ref(event) -> dict:
    return SourceRef(
        chat_id=event.chat_id,
        thread_id=event.thread_id,
        first_message_id=event.message_id,
        last_message_id=event.message_id,
    ).to_dict()


def _too_weak(det: Detection) -> bool:
    """A title only from message_text below the confidence floor is unreliable."""
    return (
        det.has_title
        and det.title_source == "message_text"
        and det.confidence < settings.classify_min_confidence
    )


# --------------------------------------------------------------------------- #
# Ingest worker
# --------------------------------------------------------------------------- #
async def ingest_worker() -> None:
    while True:
        event_id = await ingest_queue.get()
        try:
            await EventRepository.set_stage(event_id, EventStage.PROCESSING.value)
            await processing_queue.put(event_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("ingest_worker failed for %s: %s", event_id, exc)
            await EventRepository.set_stage(event_id, EventStage.ERROR.value, error=str(exc))
        finally:
            ingest_queue.task_done()


# --------------------------------------------------------------------------- #
# Processing worker
# --------------------------------------------------------------------------- #
async def processing_worker(registry: ProviderRegistry) -> None:
    while True:
        event_id = await processing_queue.get()
        try:
            await _process_event(event_id, registry)
        except Exception as exc:
            log.exception("processing_worker failed for %s: %s", event_id, exc)
            await EventRepository.set_stage(event_id, EventStage.ERROR.value, error=str(exc))
        finally:
            processing_queue.task_done()


async def _process_event(event_id: str, registry: ProviderRegistry) -> None:
    event = await EventRepository.get(event_id)
    if event is None:
        return
    if event.is_bot:
        await EventRepository.set_stage(event_id, EventStage.IGNORED.value)
        return

    det = classify(event.file_name, event.caption, event.message_text)

    if not det.has_title and not det.only_episode:
        await PendingRepository.add(event, "no title and no episode")
        await EventRepository.set_stage(event_id, EventStage.PENDING.value,
                                        classification=_clf(det))
        return
    if _too_weak(det):
        await PendingRepository.add(event, "weak message_text title below confidence floor")
        await EventRepository.set_stage(event_id, EventStage.PENDING.value,
                                        classification=_clf(det))
        return

    st = await ThreadStateRepository.get_or_create(event.chat_id, event.thread_id)
    decision = ctx.decide(st, det)

    if decision.action == ctx.ACTION_UNRESOLVED:
        await PendingRepository.add(event, decision.reason or "unresolved")
        await EventRepository.set_stage(event_id, EventStage.PENDING.value,
                                        classification=_clf(det))
        return

    if decision.action == ctx.ACTION_NEW_MEDIA:
        media = await _create_media(decision, det, registry)
        await ctx.activate(st, media._id, media.title, media.media_type)
        st.last_event_id = event._id
        await ThreadStateRepository.save(st)
    else:
        media = await MediaRepository.get(decision.media_id)
        if media is None:
            # Active context points to a missing media -> re-queue as pending.
            await PendingRepository.add(event, "active media missing")
            await EventRepository.set_stage(event_id, EventStage.PENDING.value,
                                            classification=_clf(det))
            return

    # Attach the concrete file (episode release / film release) and source.
    await _attach_payload(media, decision, event)
    await MediaRepository.add_source(media._id, _source_ref(event))
    await ctx.note_episode(st, decision.season, decision.episode)

    await EventRepository.set_stage(event_id, EventStage.PROCESSED.value,
                                    classification=_clf(det))
    await update_queue.put(media._id)


async def _create_media(decision, det: Detection,
                        registry: ProviderRegistry) -> Media:
    media_type = MediaType.coerce(decision.create_type)
    query = decision.create_title
    resolve: ResolveResult = await registry.resolve(query, media_type, decision.create_year)

    if resolve.found and resolve.metadata is not None:
        meta = resolve.metadata
        media = Media(
            media_type=media_type,
            title=meta.title or decision.create_title,
            year=meta.year or decision.create_year,
            original_title=meta.original_title,
            overview=meta.overview,
            genres=list(meta.genres),
            rating=meta.rating,
            votes=meta.votes,
            release_date=meta.release_date,
            runtime=meta.runtime,
            poster_url=meta.poster_url,
            tags=list(det.tags),
            providers={meta.provider: meta.external_id} if meta.external_id else {},
            provider_used=resolve.provider,
            metadata_resolved=resolve.matched,
        )
    else:
        # No external data yet; create from detection so episodes can bind and a
        # card can render. The healer will retry resolution later.
        media = Media(
            media_type=media_type,
            title=decision.create_title,
            year=decision.create_year,
            tags=list(det.tags),
            metadata_resolved=False,
        )
    return await MediaRepository.upsert_merge(media)


async def _attach_payload(media: Media, decision, event) -> None:
    has_episode = decision.episode is not None
    is_series_like = media.media_type in (MediaType.SERIES, MediaType.ANIME)

    if has_episode and is_series_like:
        release = _build_release(event) if event.file_name else None
        ep = Episode(
            media_id=media._id,
            season=decision.season or 1,
            episode=decision.episode or 1,
            releases=[release] if release else [],
        )
        await EpisodeRepository.upsert_merge(ep)
    elif event.file_name:
        # Film release, or a series/anime file without a parsed episode number.
        await MediaRepository.add_film_release(media._id, _build_release(event))
    # else: a pure title/announcement message -> source already recorded.


def _clf(det: Detection) -> dict:
    return {
        "has_title": det.has_title,
        "only_episode": det.only_episode,
        "title": det.title,
        "year": det.year,
        "media_type": det.media_type.value,
        "season": det.episode.season,
        "episode": det.episode.episode,
        "confidence": det.confidence,
        "title_source": det.title_source,
        "tags": det.tags,
    }


# --------------------------------------------------------------------------- #
# Update worker
# --------------------------------------------------------------------------- #
async def update_worker(client) -> None:
    # Imported lazily to avoid a circular import at module load.
    from ..ui.card import build_card
    from ..ui.post_manager import PostManager

    manager = PostManager()
    while True:
        media_id = await update_queue.get()
        try:
            media = await MediaRepository.get(media_id)
            if media is None:
                continue
            episodes = await EpisodeRepository.list_for_media(media_id)
            full_text = build_card(media, episodes)
            await manager.sync(client, media, full_text)
            await MediaRepository.mark_clean(media_id)
        except Exception as exc:
            log.exception("update_worker failed for %s: %s", media_id, exc)
        finally:
            update_queue.task_done()


# --------------------------------------------------------------------------- #
# Worker startup
# --------------------------------------------------------------------------- #
def start_workers(client, registry: ProviderRegistry) -> list[asyncio.Task]:
    return [
        asyncio.create_task(ingest_worker(), name="ingest"),
        asyncio.create_task(processing_worker(registry), name="processing"),
        asyncio.create_task(update_worker(client), name="update"),
    ]
