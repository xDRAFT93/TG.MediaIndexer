"""Google Books provider (title-searchable book fallback, German-biased)."""
from __future__ import annotations

import re
from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from ._bookmatch import select_best
from .base import MediaMetadata, Provider

log = get_logger("providers.googlebooks")

BASE = "https://www.googleapis.com/books/v1/volumes"


class GoogleBooksProvider(Provider):
    name = "googlebooks"

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.AUDIOBOOK

    @property
    def enabled(self) -> bool:
        return True  # works without a key (lower rate limit)

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if media_type != MediaType.AUDIOBOOK or not query:
            return None
        params = {
            "q": query,
            "maxResults": 5,
            "printType": "books",
            "langRestrict": settings.books_language,
        }
        if settings.google_books_api_key:
            params["key"] = settings.google_books_api_key
        try:
            r = await self.client.get(BASE, params=params)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("Google Books failed for %r: %s", query, exc)
            return None

        items = data.get("items") or []
        best = _pick_best(query, items)
        if best is None:
            return None
        info = best.get("volumeInfo", {})
        if not info.get("title"):
            return None

        authors = list(info.get("authors", []) or [])
        cats = list(info.get("categories", []) or [])
        return MediaMetadata(
            title=info.get("title", "") or query,
            provider=self.name,
            external_id=best.get("id", "") or "",
            original_title=info.get("subtitle", "") or "",
            year=_year(info.get("publishedDate")),
            overview=(info.get("description", "") or "").strip(),
            genres=cats,
            release_date=info.get("publishedDate", "") or "",
            poster_url=_cover(info.get("imageLinks", {})),
            authors=authors,
        )


def _pick_best(query: str, items: list) -> Optional[dict]:
    """The item whose title best matches the query (author removed); German
    editions get a small nudge on ties via select_best's author check."""
    cands = []
    for it in items:
        info = it.get("volumeInfo", {})
        cands.append({
            "title": info.get("title", ""),
            "original_title": info.get("subtitle", ""),
            "authors": list(info.get("authors", []) or []),
            "raw": it,
        })
    best, score = select_best(query, cands)
    if best is None or score < 50:
        return None
    return best["raw"]


def _year(value) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _cover(links: dict) -> str:
    for key in ("thumbnail", "smallThumbnail"):
        if links.get(key):
            return links[key].replace("http://", "https://")
    return ""
