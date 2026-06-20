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
import re
import time
from typing import Optional

from ..config import settings
from ..detection import context as ctx
from ..detection.classifier import Detection, classify
from ..detection.patterns import RELEASE_TOKENS, TRAILING_GROUP_RE, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
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
from ..util import slugify
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
        if ext.lower() in VIDEO_EXTENSIONS or ext.lower() in AUDIO_EXTENSIONS:
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


def _has_media_file(event) -> bool:
    """True only for messages carrying an actual downloadable video/document file.

    Photo-only announcements and plain-text messages never carry one, so they
    must not create a catalog entry. ``media_type_raw`` is the authoritative
    Telegram media kind set by the parser (``video``/``document`` vs
    ``photo``/``none``).
    """
    return event.media_type_raw in ("video", "document", "audio")


_TRAILER_BASE = ["trailer", "teaser", "preview", "vorschau", "promo",
                 "sample", "snippet", "ausschnitt"]
_ARCHIVE_EXTENSIONS = {
    "rar", "zip", "7z", "tar", "gz", "bz2", "xz", "tgz", "tbz2", "cab",
    "arj", "ace", "lzh", "lha", "z",
}
# Split archive parts: foo.part01.rar (ext rar already covered), foo.r00/.r01,
# foo.7z.001 / foo.zip.001, foo.001 (generic split).
_ARCHIVE_PART_RE = re.compile(r"(?i)\.(r\d{2,3}|\d{3})$|\.(7z|zip|rar)\.\d{3}$")


def _trailer_re() -> "re.Pattern":
    words = list(_TRAILER_BASE) + [w.lower() for w in settings.trailer_keywords]
    seen, uniq = set(), []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            uniq.append(re.escape(w))
    return re.compile(r"(?i)\b(" + "|".join(uniq) + r")s?\b")


def _is_trailer(event) -> bool:
    """True if the post text the video was shared with marks it as a
    trailer/preview/teaser/sample (built-in words plus TRAILER_KEYWORDS).

    The filename is intentionally NOT checked (release filenames can contain the
    word incidentally); only the human-written caption/message text counts.
    """
    text = f"{event.caption or ''} {event.message_text or ''}"
    return bool(_trailer_re().search(text))


def is_archive_name(file_name: str) -> bool:
    """True if a file name denotes a (possibly multi-part) archive."""
    fn = (file_name or "").lower()
    if not fn:
        return False
    if _ARCHIVE_PART_RE.search(fn):
        return True
    if "." in fn:
        return fn.rsplit(".", 1)[-1] in _ARCHIVE_EXTENSIONS
    return False


def _is_archive(event) -> bool:
    """True if the uploaded file is a (possibly multi-part) archive."""
    return is_archive_name(event.file_name or "")


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

    # Threads on the ignore list never produce a catalog entry (e.g. an
    # off-topic or audiobook-dump topic the owner does not want indexed).
    if event.thread_id in settings.ignore_thread_ids:
        await EventRepository.set_stage(event_id, EventStage.IGNORED.value)
        return

    # Trailers/previews must never create or update a catalog entry. If the post
    # text the video was shared with marks it as a trailer/preview/teaser/sample,
    # drop it: no entry, no post, no source tracking.
    if _is_trailer(event):
        await EventRepository.set_stage(event_id, EventStage.IGNORED.value)
        return

    # Archive uploads (rar/zip/7z, multi-part splits, …) are skipped when
    # IGNORE_ARCHIVE_FILES is on, so they don't create junk entries.
    if settings.ignore_archive_files and _is_archive(event):
        await EventRepository.set_stage(event_id, EventStage.IGNORED.value)
        return

    det = classify(event.file_name, event.caption, event.message_text)

    # Files coming from a configured anime topic are anime: bias the type so the
    # resolver tries the anime providers (Jikan/AniList/Kitsu) before TMDb/OMDb.
    if event.thread_id in settings.anime_source_threads:
        det.media_type = MediaType.ANIME
        det.anime_signal = True

    # Files from a configured audiobook topic are audiobooks: the audiobook
    # providers (Audnexus -> Google Books -> DNB -> Open Library) are tried, and
    # any TV-episode misreads of part numbers are dropped (audiobooks are
    # file/part based, never seasons).
    if event.thread_id in settings.audiobook_source_threads:
        det.media_type = MediaType.AUDIOBOOK
        det.audiobook_signal = True
        det.anime_signal = False

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

    # Only a message carrying an actual video/document file may create or update a
    # catalog entry. A file-less announcement (image + title, or plain text) must
    # NOT spawn its own entry/post: if it names a title it only primes the
    # thread's provisional context so the first real file binds to it; otherwise
    # it is ignored. This prevents the "two entries" problem where the poster post
    # and the later file post each produced a separate media record.
    if not _has_media_file(event):
        if det.has_title and not det.only_episode:
            # Use at most the first 2 lines of the announcement (image + text),
            # as requested. Each line is a separate title candidate (line 1 is
            # normally "Title (Year)") — never concatenate lines into one title,
            # which would wreck the provider search.
            pend_title, pend_type = "", det.media_type
            for line in (event.caption or event.message_text).splitlines()[:2]:
                a_det = classify("", line.strip(), "")
                if a_det.has_title:
                    pend_title, pend_type = a_det.title, a_det.media_type
                    break
            if not pend_title:
                pend_title, pend_type = det.title, det.media_type
            await ctx.set_pending(st, pend_title, pend_type)
            st.last_event_id = event._id
            await ThreadStateRepository.save(st)
            await EventRepository.set_stage(event_id, EventStage.CONTEXT.value,
                                            classification=_clf(det))
        else:
            await EventRepository.set_stage(event_id, EventStage.IGNORED.value,
                                            classification=_clf(det))
        return

    decision = ctx.decide(st, det)

    if decision.action == ctx.ACTION_UNRESOLVED:
        await PendingRepository.add(event, decision.reason or "unresolved")
        await EventRepository.set_stage(event_id, EventStage.PENDING.value,
                                        classification=_clf(det))
        return

    if decision.action == ctx.ACTION_NEW_MEDIA:
        media = await _create_media(decision, det, registry,
                                    fallback_query=st.pending_title)
        await ctx.activate(st, media._id, media.title, media.media_type,
                           resolved=media.metadata_resolved)
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
                        registry: ProviderRegistry, fallback_query: str = "") -> Media:
    media_type = MediaType.coerce(decision.create_type)

    # Build an ordered, de-duplicated list of title queries to try: every
    # candidate the detector found (file name, caption, post text) plus the
    # announcement fallback. This is what lets a cryptic file name resolve via
    # the real title in the Telegram post text.
    raw_queries = list(getattr(det, "search_titles", None) or [])
    raw_queries += [decision.create_title, fallback_query]
    queries: list[str] = []
    seen: set[str] = set()
    for q in raw_queries:
        q = (q or "").strip()
        if q and slugify(q) not in seen:
            seen.add(slugify(q))
            queries.append(q)
    if not queries:
        queries = [decision.create_title or ""]

    hints = None
    if media_type == MediaType.AUDIOBOOK:
        hints = {
            "authors": list(getattr(det, "authors", None) or []),
            "volume": getattr(det, "volume", None),
            "language": settings.books_language,
        }

    results: list[tuple[str, ResolveResult]] = []
    matched: Optional[tuple[str, ResolveResult]] = None
    for q in queries:
        r = await registry.resolve(q, media_type, decision.create_year, hints)
        results.append((q, r))
        if r.matched:
            matched = (q, r)
            break

    if matched:
        query, resolve = matched
        if query != queries[0]:
            log.info("Resolved via fallback title '%s' (file title '%s').",
                     query, queries[0])
    else:
        # No strong match: keep the first partial (provider hint) if any.
        partial = next(((q, r) for q, r in results if r.found), None)
        query, resolve = partial if partial else results[0]

    if resolve.found and resolve.metadata is not None:
        meta = resolve.metadata
        media = Media(
            media_type=media_type,
            title=meta.title or query,
            year=meta.year or decision.create_year,
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
            tags=list(det.tags),
            search_aliases=queries,
            providers={meta.provider: meta.external_id} if meta.external_id else {},
            provider_used=resolve.provider,
            metadata_resolved=resolve.matched,
        )
    else:
        # No external data yet; create from detection so episodes can bind and a
        # card can render. Use the most descriptive title for display and keep all
        # query candidates so .repair can re-try resolution from the post text.
        media = Media(
            media_type=media_type,
            title=decision.create_title or query,
            year=decision.create_year,
            tags=list(det.tags),
            search_aliases=queries,
            metadata_resolved=False,
        )
    return await MediaRepository.upsert_merge(media)


async def _attach_payload(media: Media, decision, event) -> None:
    has_episode = decision.episode is not None
    is_series_like = media.media_type in (MediaType.SERIES, MediaType.ANIME)

    if has_episode and is_series_like:
        # Build a release whenever the message carries an actual file — even a
        # video sent WITHOUT a filename (common for anime). Without this the
        # episode would have no source release and would render as plain,
        # unclickable text. The file_name may be empty; dedup then falls back to
        # the message id.
        release = _build_release(event) if _has_media_file(event) else None
        ep = Episode(
            media_id=media._id,
            season=decision.season or 1,
            episode=decision.episode or 1,
            releases=[release] if release else [],
        )
        await EpisodeRepository.upsert_merge(ep)
    elif _has_media_file(event):
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
_update_recent: dict[str, list[float]] = {}   # timestamps -> repost-loop detection
_update_fails: dict[str, int] = {}             # consecutive hard failures per media
_update_quarantine: set[str] = set()           # media skipped after a loop/timeout


async def update_worker(client) -> None:
    # Imported lazily to avoid a circular import at module load.
    from ..ui.card import build_card
    from ..ui.post_manager import PostManager

    manager = PostManager()
    while True:
        media_id = await update_queue.get()
        try:
            if media_id in _update_quarantine:
                # Permanently skipped this session to break a loop/hang.
                await MediaRepository.mark_clean(media_id)
                continue

            # Loop guard: if the SAME media is reprocessed too many times in a
            # short window, something is re-dirtying it (the field-reported
            # post→delete→post loop). Quarantine it and move on instead of
            # spinning forever.
            now = time.monotonic()
            hist = [t for t in _update_recent.get(media_id, []) if now - t < 120.0]
            hist.append(now)
            _update_recent[media_id] = hist
            if len(hist) > settings.update_loop_max:
                log.error("Quarantining media %s: reprocessed %d×/120s (loop guard).",
                          media_id, len(hist))
                _update_quarantine.add(media_id)
                await MediaRepository.mark_clean(media_id)
                continue

            media = await MediaRepository.get(media_id)
            if media is None:
                continue
            if settings.post_only_if_resolved and not media.metadata_resolved:
                log.info("Not posting unresolved media %s (POST_ONLY_IF_RESOLVED).",
                         media_id)
                await MediaRepository.mark_clean(media_id)
                continue

            episodes = await EpisodeRepository.list_for_media(media_id)
            full_text = build_card(media, episodes)
            # Hard timeout so a hang on one entry never blocks the whole worker.
            await asyncio.wait_for(
                manager.sync(client, media, full_text),
                timeout=settings.update_sync_timeout,
            )
            await MediaRepository.mark_clean(media_id)
            _update_fails.pop(media_id, None)  # success resets the failure count
        except asyncio.TimeoutError:
            log.error("update_worker timeout for %s after %ss — skipping.",
                      media_id, settings.update_sync_timeout)
            _update_quarantine.add(media_id)
            try:
                await MediaRepository.mark_clean(media_id)
            except Exception:
                pass
        except Exception as exc:
            log.exception("update_worker failed for %s: %s", media_id, exc)
            n = _update_fails.get(media_id, 0) + 1
            _update_fails[media_id] = n
            if n >= settings.update_fail_max:
                log.error("Quarantining media %s after %d consecutive failures.",
                          media_id, n)
                _update_quarantine.add(media_id)
                try:
                    await MediaRepository.mark_clean(media_id)
                except Exception:
                    pass
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
