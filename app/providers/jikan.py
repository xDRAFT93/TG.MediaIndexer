"""Jikan provider (MyAnimeList).

Highest-priority source for anime. Jikan is the unofficial read-only MyAnimeList
API and needs no authentication. Because anime titles are messy (romaji vs.
english vs. native), several candidates are fetched and the best one is picked
by fuzzy title similarity instead of blindly taking the first hit.
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import settings
from ..detection.confidence import title_similarity
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.jikan")

BASE = "https://api.jikan.moe/v4"
_DURATION_RE = re.compile(r"(\d+)\s*min")


def _duration_minutes(raw: str) -> Optional[int]:
    if not raw:
        return None
    m = _DURATION_RE.search(raw)
    return int(m.group(1)) if m else None


class JikanProvider(Provider):
    name = "jikan"

    @property
    def enabled(self) -> bool:
        # No key required; always available.
        return True

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.ANIME

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int],
                     hints: Optional[dict] = None) -> Optional[MediaMetadata]:
        if not query:
            return None
        params = {"q": query, "limit": 5, "sfw": "false"}
        try:
            r = await self.client.get(f"{BASE}/anime", params=params)
            if r.status_code != 200:
                return None
            data = r.json().get("data", [])
        except Exception as exc:  # pragma: no cover - network failure path
            log.warning("Jikan search failed for %r: %s", query, exc)
            return None
        if not data:
            return None

        best = self._pick_best(query, data)
        if best is None:
            return None
        return self._to_metadata(best)

    def _pick_best(self, query: str, data: list[dict]) -> Optional[dict]:
        best, best_score = None, -1.0
        for item in data:
            candidates = [
                item.get("title") or "",
                item.get("title_english") or "",
                item.get("title_japanese") or "",
            ]
            score = max((title_similarity(query, c) for c in candidates if c), default=0.0)
            if score > best_score:
                best, best_score = item, score
        return best

    def _to_metadata(self, d: dict) -> MediaMetadata:
        aired = (d.get("aired") or {}).get("from") or ""
        release_date = aired[:10] if aired else ""
        year = d.get("year")
        if not year and release_date[:4].isdigit():
            year = int(release_date[:4])
        genres = [g.get("name", "") for g in d.get("genres", []) if g.get("name")]
        images = (d.get("images") or {}).get("jpg") or {}
        poster = images.get("large_image_url") or images.get("image_url") or ""
        return MediaMetadata(
            title=d.get("title_english") or d.get("title") or "",
            provider=self.name,
            external_id=str(d.get("mal_id", "")),
            original_title=d.get("title") or "",
            year=year,
            overview=d.get("synopsis", "") or "",
            genres=genres,
            rating=d.get("score"),
            votes=d.get("scored_by"),
            release_date=release_date,
            runtime=_duration_minutes(d.get("duration", "")),
            poster_url=poster,
        )
