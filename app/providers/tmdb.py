"""TMDb provider (films and series)."""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.tmdb")

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"


class TMDbProvider(Provider):
    name = "tmdb"

    def __init__(self, client) -> None:
        super().__init__(client)
        self._genre_cache: dict[str, dict[int, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(settings.tmdb_api_key)

    def supports(self, media_type: MediaType) -> bool:
        return True  # also usable as anime fallback

    async def _genres(self, kind: str) -> dict[int, str]:
        if kind in self._genre_cache:
            return self._genre_cache[kind]
        mapping: dict[int, str] = {}
        try:
            r = await self.client.get(
                f"{BASE}/genre/{kind}/list",
                params={"api_key": settings.tmdb_api_key, "language": settings.tmdb_language},
            )
            if r.status_code == 200:
                for g in r.json().get("genres", []):
                    mapping[g["id"]] = g["name"]
        except Exception as exc:  # pragma: no cover
            log.warning("TMDb genre fetch failed: %s", exc)
        self._genre_cache[kind] = mapping
        return mapping

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if not self.enabled or not query:
            return None
        kind = "movie" if media_type == MediaType.FILM else "tv"
        params = {
            "api_key": settings.tmdb_api_key,
            "language": settings.tmdb_language,
            "query": query,
            "include_adult": "true",
        }
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        try:
            r = await self.client.get(f"{BASE}/search/{kind}", params=params)
            if r.status_code != 200:
                return None
            results = r.json().get("results", [])
            if not results:
                return None
            top = results[0]
            return await self._details(kind, top["id"])
        except Exception as exc:  # pragma: no cover
            log.warning("TMDb search failed for %r: %s", query, exc)
            return None

    async def _details(self, kind: str, tmdb_id: int) -> Optional[MediaMetadata]:
        try:
            r = await self.client.get(
                f"{BASE}/{kind}/{tmdb_id}",
                params={"api_key": settings.tmdb_api_key, "language": settings.tmdb_language},
            )
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception:  # pragma: no cover
            return None

        if kind == "movie":
            title = d.get("title") or d.get("original_title") or ""
            release = d.get("release_date", "") or ""
            runtime = d.get("runtime")
        else:
            title = d.get("name") or d.get("original_name") or ""
            release = d.get("first_air_date", "") or ""
            ert = d.get("episode_run_time") or []
            runtime = ert[0] if ert else None

        genres = [g.get("name", "") for g in d.get("genres", []) if g.get("name")]
        year = None
        if release[:4].isdigit():
            year = int(release[:4])
        poster = f"{IMG}{d['poster_path']}" if d.get("poster_path") else ""

        return MediaMetadata(
            title=title,
            provider=self.name,
            external_id=str(tmdb_id),
            original_title=d.get("original_title") or d.get("original_name") or "",
            year=year,
            overview=d.get("overview", "") or "",
            genres=genres,
            rating=d.get("vote_average"),
            votes=d.get("vote_count"),
            release_date=release,
            runtime=runtime,
            poster_url=poster,
        )
