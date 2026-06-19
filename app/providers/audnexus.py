"""Audnexus audiobook provider (primary).

Audnexus (https://api.audnex.us) is keyed by Audible ASIN, not free text, so it
is used whenever an ASIN can be recovered from the file name / post text (audio
releases frequently embed one, e.g. ``B07XYZ1234``). Region defaults to ``de``
for German Audible metadata. When no ASIN is present this provider yields
nothing and the registry falls through to the title-searchable book providers
(Google Books, DNB, Open Library).
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.audnexus")

BASE = "https://api.audnex.us"
# Audible ASINs are 10-char, start with B0, uppercase alnum.
ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")


def extract_asin(text: str) -> str:
    m = ASIN_RE.search((text or "").upper())
    return m.group(1) if m else ""


class AudnexusProvider(Provider):
    name = "audnexus"

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.AUDIOBOOK

    @property
    def enabled(self) -> bool:
        return True  # no key required

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if media_type != MediaType.AUDIOBOOK:
            return None
        asin = extract_asin(query)
        if not asin:
            return None  # ASIN-only API -> let the next provider title-search
        url = f"{BASE}/books/{asin}"
        try:
            r = await self.client.get(url, params={"region": settings.audnexus_region})
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("Audnexus failed for %s: %s", asin, exc)
            return None
        if not d or not d.get("title"):
            return None

        authors = [a.get("name", "") for a in d.get("authors", []) if a.get("name")]
        narrators = [n.get("name", "") for n in d.get("narrators", []) if n.get("name")]
        genres = [g.get("name", "") for g in d.get("genres", []) if g.get("name")]
        return MediaMetadata(
            title=d.get("title", "") or query,
            provider=self.name,
            external_id=d.get("asin", asin),
            original_title=d.get("subtitle", "") or "",
            year=_year(d.get("releaseDate")),
            overview=_clean(d.get("summary") or d.get("description") or ""),
            genres=genres,
            release_date=d.get("releaseDate", "") or "",
            runtime=_minutes(d.get("runtimeLengthMin")),
            poster_url=d.get("image", "") or "",
            authors=authors,
            narrator=", ".join(narrators),
        )


def _year(value) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _minutes(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()
