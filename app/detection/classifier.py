"""Classifier.

Combines the per-field extractions under the strict title priority:
    media.file_name  >  caption  >  message_text  >  (thread context, elsewhere)

and decides the media type (film / series / anime) with a confidence score.
Hashtags are tags, never titles. Episode-only messages are flagged for binding
to the thread's active media (handled by the context manager).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from .episodes import EpisodeInfo
from ..storage.models import MediaType
from .extractor import Extraction, extract_from_filename, extract_from_text


@dataclass
class Detection:
    has_title: bool = False
    only_episode: bool = False
    title: str = ""
    year: Optional[int] = None
    media_type: MediaType = MediaType.FILM
    episode: EpisodeInfo = field(default_factory=EpisodeInfo)
    tags: list[str] = field(default_factory=list)
    anime_signal: bool = False
    confidence: float = 0.0
    title_source: str = ""

    @property
    def provider_query(self) -> str:
        if self.year:
            return f"{self.title} {self.year}".strip()
        return self.title.strip()


_TITLE_SOURCE_WEIGHT = {
    "file_name": 0.90,
    "caption": 0.70,
    "message_text": 0.55,
}


def classify(file_name: str, caption: str, message_text: str) -> Detection:
    extractions: list[Extraction] = []
    if file_name:
        extractions.append(extract_from_filename(file_name))
    if caption:
        extractions.append(extract_from_text(caption, "caption"))
    if message_text:
        extractions.append(extract_from_text(message_text, "message_text"))

    tags: list[str] = []
    anime_signal = False
    for ex in extractions:
        for t in ex.tags:
            if t not in tags:
                tags.append(t)
        anime_signal = anime_signal or ex.anime_signal

    # Episode: prefer a source with full season+episode, then any episode, then a
    # season-only signal — scanning sources in priority order (file/caption/text).
    episode = EpisodeInfo()
    for ex in extractions:
        if ex.episode.has_episode and ex.episode.season is not None:
            episode = ex.episode
            break
    if not episode.has_episode:
        for ex in extractions:
            if ex.episode.has_episode:
                episode = ex.episode
                break
    if not episode.has_any:
        for ex in extractions:
            if ex.episode.has_any:
                episode = ex.episode
                break

    anime_by_tag = any(t == "anime" for t in tags)
    anime_signal = anime_signal or anime_by_tag

    has_marker = any(ex.has_own_marker for ex in extractions)

    # Series-title selection. The decisive rule: when an episode marker exists
    # ANYWHERE, the series title is the text BEFORE the marker, taken from the
    # highest-priority source that itself carried the marker. A source whose only
    # "title" is really the episode title (no marker of its own) is NOT used as a
    # series name. If no marker-bearing source yields a title, this is an
    # episode-only message that must bind to the thread's series.
    if has_marker:
        chosen = next((ex for ex in extractions
                       if ex.has_own_marker and ex.has_title), None)
    else:
        chosen = next((ex for ex in extractions if ex.has_title), None)

    # Year: from the chosen source, else from any source that carries one.
    year = chosen.year if chosen else None
    if year is None:
        for ex in extractions:
            if ex.year:
                year = ex.year
                break

    if chosen is None:
        # No usable series title. With an episode -> bind to the thread context.
        if episode.has_episode:
            return Detection(
                has_title=False,
                only_episode=True,
                episode=episode,
                year=year,
                tags=tags,
                anime_signal=anime_signal,
                confidence=0.5,
            )
        return Detection(has_title=False, only_episode=False, year=year, tags=tags,
                         anime_signal=anime_signal, confidence=0.0)

    media_type, type_conf = _decide_type(chosen, episode, anime_signal)

    base = _TITLE_SOURCE_WEIGHT.get(chosen.source_field, 0.5)
    confidence = base + (0.08 if year else 0.0)
    confidence = min(1.0, confidence * (0.6 + 0.4 * type_conf))

    return Detection(
        has_title=True,
        only_episode=False,
        title=chosen.title,
        year=year,
        media_type=media_type,
        episode=episode,
        tags=tags,
        anime_signal=anime_signal,
        confidence=round(confidence, 3),
        title_source=chosen.source_field,
    )


def _decide_type(chosen: Extraction, episode: EpisodeInfo,
                 anime_signal: bool) -> tuple[MediaType, float]:
    if anime_signal:
        return MediaType.ANIME, 0.85
    if episode.has_episode or episode.season is not None:
        return MediaType.SERIES, 0.75
    return MediaType.FILM, 0.6
