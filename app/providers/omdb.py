"""OMDb provider (IMDb-backed fallback for films and series)."""
from __future__ import annotations

import re
from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.omdb")

BASE = "https://www.omdbapi.com/"


class OMDbProvider(Provider):
    name = "omdb"

    @property
    def enabled(self) -> bool:
        return bool(settings.omdb_api_key)

    def supports(self, media_type: MediaType) -> bool:
        return True

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if not self.enabled or not query:
            return None
        otype = {"film": "movie", "series": "series", "anime": "series"}.get(
            media_type.value, ""
        )
        params = {"apikey": settings.omdb_api_key, "t": query}
        if year:
            params["y"] = year
        if otype:
            params["type"] = otype
        try:
            r = await self.client.get(BASE, params=params)
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception as exc:  # pragma: no cover
            log.warning("OMDb search failed for %r: %s", query, exc)
            return None

        if d.get("Response") != "True":
            return None

        genres = [g.strip() for g in (d.get("Genre", "") or "").split(",") if g.strip()]
        return MediaMetadata(
            title=d.get("Title", "") or query,
            provider=self.name,
            external_id=d.get("imdbID", "") or "",
            year=_year(d.get("Year")),
            overview=d.get("Plot", "") if d.get("Plot") not in ("N/A", None) else "",
            genres=genres,
            rating=_float(d.get("imdbRating")),
            votes=_int(d.get("imdbVotes")),
            release_date=d.get("Released", "") if d.get("Released") != "N/A" else "",
            runtime=_runtime(d.get("Runtime")),
            poster_url=d.get("Poster", "") if d.get("Poster") != "N/A" else "",
        )


def _year(value) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def _runtime(value) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else None
