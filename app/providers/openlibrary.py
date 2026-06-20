"""Open Library provider (last-resort book fallback)."""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from ._bookmatch import select_best
from .base import MediaMetadata, Provider

log = get_logger("providers.openlibrary")

BASE = "https://openlibrary.org/search.json"
_LANG3 = {"de": "ger", "en": "eng", "fr": "fre", "es": "spa", "it": "ita"}


class OpenLibraryProvider(Provider):
    name = "openlibrary"

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.AUDIOBOOK

    @property
    def enabled(self) -> bool:
        return True  # no key required

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int],
                     hints: Optional[dict] = None) -> Optional[MediaMetadata]:
        if media_type != MediaType.AUDIOBOOK or not query:
            return None
        params = {"q": query, "limit": 5,
                  "fields": "title,author_name,first_publish_year,cover_i,subject,key,language"}
        try:
            r = await self.client.get(BASE, params=params)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("Open Library failed for %r: %s", query, exc)
            return None

        docs = data.get("docs") or []
        doc = _pick(query, docs)
        if doc is None or not doc.get("title"):
            return None
        cover_id = doc.get("cover_i")
        return MediaMetadata(
            title=doc.get("title", "") or query,
            provider=self.name,
            external_id=doc.get("key", "") or "",
            year=doc.get("first_publish_year"),
            genres=list(doc.get("subject", [])[:5] or []),
            poster_url=(f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
                        if cover_id else ""),
            authors=list(doc.get("author_name", []) or []),
        )


def _pick(query: str, docs: list) -> Optional[dict]:
    cands = []
    for d in docs:
        cands.append({
            "title": d.get("title", ""),
            "original_title": "",
            "authors": list(d.get("author_name", []) or []),
            "raw": d,
        })
    best, score = select_best(query, cands)
    if best is None or score < 50:
        return None
    return best["raw"]
