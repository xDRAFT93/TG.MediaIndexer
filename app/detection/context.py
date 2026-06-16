"""Thread / episode context.

Implements the critical rule: once a media title is recognised in a thread it
becomes the *active context*. Subsequent episode-only messages are bound to it.
Episodes are never standalone media while an active title exists.

All state lives in MongoDB (thread_state); there is no RAM-only context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import settings
from ..storage.models import MediaType, ThreadState
from ..storage.repositories import ThreadStateRepository
from .classifier import Detection
from .confidence import title_similarity

ACTION_NEW_MEDIA = "new_media"
ACTION_SAME_MEDIA = "same_media"
ACTION_BIND_EPISODE = "bind_episode"
ACTION_UNRESOLVED = "unresolved"


@dataclass
class ContextDecision:
    action: str
    media_id: str = ""
    create_title: str = ""
    create_year: Optional[int] = None
    create_type: MediaType = MediaType.FILM
    season: Optional[int] = None
    episode: Optional[int] = None
    reason: str = ""


def decide(st: ThreadState, det: Detection) -> ContextDecision:
    """Pure decision: does this message start a new media, extend the active
    one, attach an episode, or is it unresolved?"""
    if det.has_title:
        same = bool(st.active_media_id) and (
            title_similarity(det.title, st.active_title) >= settings.title_match_threshold
        )
        if same:
            season = det.episode.season or st.season_cursor or 1
            return ContextDecision(
                action=ACTION_SAME_MEDIA,
                media_id=st.active_media_id,
                season=season,
                episode=det.episode.episode,
            )
        # New / switched media. Episode (if any) belongs to the new media.
        return ContextDecision(
            action=ACTION_NEW_MEDIA,
            create_title=det.title,
            create_year=det.year,
            create_type=det.media_type,
            season=det.episode.season or 1,
            episode=det.episode.episode,
        )

    if det.only_episode and det.episode.has_episode:
        if st.active_media_id:
            season = det.episode.season or st.season_cursor or 1
            return ContextDecision(
                action=ACTION_BIND_EPISODE,
                media_id=st.active_media_id,
                season=season,
                episode=det.episode.episode,
            )
        if st.pending_title:
            # An earlier file-less announcement named the series; create the media
            # now from that provisional title and attach this episode to it. This
            # is what keeps the announcement from spawning its own empty entry.
            # The presence of an episode means it is series-like, never a film.
            ptype = MediaType.ANIME if st.pending_type == MediaType.ANIME.value else MediaType.SERIES
            return ContextDecision(
                action=ACTION_NEW_MEDIA,
                create_title=st.pending_title,
                create_type=ptype,
                season=det.episode.season or 1,
                episode=det.episode.episode,
            )
        return ContextDecision(
            action=ACTION_UNRESOLVED,
            reason="episode without active media context",
        )

    return ContextDecision(action=ACTION_UNRESOLVED, reason="no title and no episode")


async def activate(st: ThreadState, media_id: str, title: str,
                   media_type: MediaType) -> None:
    """Set the active media for the thread (resets episode cursors)."""
    st.active_media_id = media_id
    st.active_title = title
    st.active_media_type = media_type.value
    st.pending_title = ""        # provisional context consumed
    st.pending_type = ""
    st.episode_cursor = 0
    st.season_cursor = 1
    await ThreadStateRepository.save(st)


async def set_pending(st: ThreadState, title: str, media_type: MediaType) -> None:
    """Remember a provisional title from a file-less announcement without
    creating any media. The next real file in the thread will use it."""
    st.pending_title = title
    st.pending_type = media_type.value
    await ThreadStateRepository.save(st)


async def note_episode(st: ThreadState, season: Optional[int],
                       episode: Optional[int]) -> None:
    """Advance season/episode cursors after attaching an episode."""
    changed = False
    if season and season != st.season_cursor:
        st.season_cursor = season
        changed = True
    if episode and episode > st.episode_cursor:
        st.episode_cursor = episode
        changed = True
    if changed:
        await ThreadStateRepository.save(st)


def next_sequential_episode(st: ThreadState) -> int:
    """For binding episodes that lack an explicit number: continue counting."""
    return st.episode_cursor + 1
