"""AniList provider (anime).

Secondary anime source via the public AniList GraphQL API (no key required).
"""
from __future__ import annotations

import re
from typing import Optional

from ..detection.confidence import title_similarity
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.anilist")

ENDPOINT = "https://graphql.anilist.co"
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      description(asHtml: false)
      genres
      averageScore
      popularity
      episodes
      duration
      startDate { year month day }
      coverImage { large extraLarge }
    }
  }
}
"""


def _strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub("", text)
    return cleaned.replace("&nbsp;", " ").replace("\r", " ").strip()


def _date_str(node: dict) -> str:
    y, m, d = node.get("year"), node.get("month"), node.get("day")
    if not y:
        return ""
    return f"{y:04d}-{(m or 1):02d}-{(d or 1):02d}"


class AniListProvider(Provider):
    name = "anilist"

    @property
    def enabled(self) -> bool:
        return True

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.ANIME

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if not query:
            return None
        try:
            r = await self.client.post(
                ENDPOINT,
                json={"query": _QUERY, "variables": {"search": query}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if r.status_code != 200:
                return None
            media = (((r.json() or {}).get("data") or {}).get("Page") or {}).get("media") or []
        except Exception as exc:  # pragma: no cover - network failure path
            log.warning("AniList search failed for %r: %s", query, exc)
            return None
        if not media:
            return None

        best = self._pick_best(query, media)
        if best is None:
            return None
        return self._to_metadata(best)

    def _pick_best(self, query: str, media: list[dict]) -> Optional[dict]:
        best, best_score = None, -1.0
        for item in media:
            title = item.get("title") or {}
            candidates = [title.get("english"), title.get("romaji"), title.get("native")]
            score = max((title_similarity(query, c) for c in candidates if c), default=0.0)
            if score > best_score:
                best, best_score = item, score
        return best

    def _to_metadata(self, d: dict) -> MediaMetadata:
        title = d.get("title") or {}
        start = d.get("startDate") or {}
        release_date = _date_str(start)
        avg = d.get("averageScore")
        rating = round(avg / 10.0, 1) if isinstance(avg, (int, float)) else None
        cover = d.get("coverImage") or {}
        poster = cover.get("extraLarge") or cover.get("large") or ""
        return MediaMetadata(
            title=title.get("english") or title.get("romaji") or "",
            provider=self.name,
            external_id=str(d.get("id", "")),
            original_title=title.get("romaji") or title.get("native") or "",
            year=start.get("year"),
            overview=_strip_html(d.get("description", "")),
            genres=list(d.get("genres") or []),
            rating=rating,
            votes=d.get("popularity"),
            release_date=release_date,
            runtime=d.get("duration"),
            poster_url=poster,
        )
