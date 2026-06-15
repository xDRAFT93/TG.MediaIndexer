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

    # Choose title by strict priority order of the fields present.
    chosen: Optional[Extraction] = None
    for ex in extractions:
        if ex.has_title:
            chosen = ex
            break

    # Episode info: take from the chosen field, else any field that has it.
    episode = EpisodeInfo()
    for ex in extractions:
        if ex.episode.has_any:
            episode = ex.episode
            break
    if chosen and chosen.episode.has_any:
        episode = chosen.episode

    anime_by_tag = any(t in {"anime", " animes"} or t == "anime" for t in tags)
    anime_signal = anime_signal or anime_by_tag

    if chosen is None:
        # No title anywhere. If there is episode info -> bind to context.
        if episode.has_episode:
            return Detection(
                has_title=False,
                only_episode=True,
                episode=episode,
                tags=tags,
                anime_signal=anime_signal,
                confidence=0.5,
            )
        # Nothing usable.
        return Detection(has_title=False, only_episode=False, tags=tags,
                         anime_signal=anime_signal, confidence=0.0)

    media_type, type_conf = _decide_type(chosen, episode, anime_signal)

    base = _TITLE_SOURCE_WEIGHT.get(chosen.source_field, 0.5)
    year = chosen.year
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
