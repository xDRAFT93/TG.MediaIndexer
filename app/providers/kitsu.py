"""Kitsu provider (anime).

Tertiary anime source via the Kitsu JSON:API (no key required). Genres require
a separate relationship call and are intentionally skipped here to keep the
import fast; TMDb/OMDb in the chain can still supply genres as a fallback.
"""
from __future__ import annotations

from typing import Optional

from ..detection.confidence import title_similarity
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.kitsu")

BASE = "https://kitsu.io/api/edge"


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KitsuProvider(Provider):
    name = "kitsu"

    @property
    def enabled(self) -> bool:
        return True

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.ANIME

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int],
                     hints: Optional[dict] = None) -> Optional[MediaMetadata]:
        if not query:
            return None
        params = {"filter[text]": query, "page[limit]": 5}
        headers = {"Accept": "application/vnd.api+json"}
        try:
            r = await self.client.get(f"{BASE}/anime", params=params, headers=headers)
            if r.status_code != 200:
                return None
            data = r.json().get("data", [])
        except Exception as exc:  # pragma: no cover - network failure path
            log.warning("Kitsu search failed for %r: %s", query, exc)
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
            attrs = item.get("attributes") or {}
            titles = attrs.get("titles") or {}
            candidates = [
                attrs.get("canonicalTitle"),
                titles.get("en"),
                titles.get("en_jp"),
                titles.get("ja_jp"),
            ]
            score = max((title_similarity(query, c) for c in candidates if c), default=0.0)
            if score > best_score:
                best, best_score = item, score
        return best

    def _to_metadata(self, item: dict) -> MediaMetadata:
        attrs = item.get("attributes") or {}
        start = attrs.get("startDate") or ""
        year = int(start[:4]) if start[:4].isdigit() else None
        avg = _to_float(attrs.get("averageRating"))
        rating = round(avg / 10.0, 1) if avg is not None else None
        poster_obj = attrs.get("posterImage") or {}
        poster = poster_obj.get("original") or poster_obj.get("large") or ""
        return MediaMetadata(
            title=attrs.get("canonicalTitle") or "",
            provider=self.name,
            external_id=str(item.get("id", "")),
            original_title=(attrs.get("titles") or {}).get("ja_jp") or "",
            year=year,
            overview=attrs.get("synopsis", "") or "",
            genres=[],
            rating=rating,
            votes=attrs.get("userCount"),
            release_date=start,
            runtime=attrs.get("episodeLength"),
            poster_url=poster,
        )
