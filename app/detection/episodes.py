"""Parse season/episode markers from a string."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .patterns import EPISODE_ONLY_RES, SEASON_EPISODE_RES, SEASON_ONLY_RES


@dataclass
class EpisodeInfo:
    season: Optional[int] = None
    episode: Optional[int] = None

    @property
    def has_episode(self) -> bool:
        return self.episode is not None

    @property
    def has_any(self) -> bool:
        return self.episode is not None or self.season is not None


def parse_episode(text: str) -> EpisodeInfo:
    if not text:
        return EpisodeInfo()

    for rx in SEASON_EPISODE_RES:
        m = rx.search(text)
        if m:
            gd = m.groupdict()
            return EpisodeInfo(
                season=_to_int(gd.get("season")),
                episode=_to_int(gd.get("episode")),
            )

    season: Optional[int] = None
    for rx in SEASON_ONLY_RES:
        m = rx.search(text)
        if m:
            season = _to_int(m.groupdict().get("season"))
            break

    for rx in EPISODE_ONLY_RES:
        m = rx.search(text)
        if m:
            return EpisodeInfo(season=season, episode=_to_int(m.groupdict().get("episode")))

    return EpisodeInfo(season=season, episode=None)


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
