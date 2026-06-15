"""Provider registry.

Owns one shared ``httpx.AsyncClient`` and the provider instances, and resolves
metadata for a query by walking a media-type specific fallback chain:

    anime         -> jikan -> anilist -> kitsu -> tmdb -> omdb
    film / series -> tmdb  -> omdb

External priorities follow the spec: for anime MyAnimeList (Jikan) is tried
first, then the other anime engines, and only then TMDb/OMDb as a fallback.
Every provider call is wrapped by a persistent cache so repeated imports do not
hammer the external APIs. The first result whose title is similar enough to the
query (>= PROVIDER_MATCH_THRESHOLD) wins; otherwise the best partial match is
kept with ``metadata_resolved`` still set so the healer can retry later.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .anilist import AniListProvider
from .base import MediaMetadata, Provider
from .jikan import JikanProvider
from .kitsu import KitsuProvider
from .omdb import OMDbProvider
from .tmdb import TMDbProvider
from ..config import settings
from ..detection.confidence import title_similarity
from ..logging_setup import get_logger
from ..storage.models import MediaType
from ..storage.repositories import ProviderCacheRepository

log = get_logger("providers.registry")

_USER_AGENT = "MediaIndexer/1.0 (+https://localhost; private media wiki)"


class ResolveResult:
    """Outcome of a metadata resolution attempt."""

    def __init__(self, metadata: Optional[MediaMetadata], provider: str,
                 score: float, matched: bool) -> None:
        self.metadata = metadata
        self.provider = provider
        self.score = score
        self.matched = matched

    @property
    def found(self) -> bool:
        return self.metadata is not None


class ProviderRegistry:
    def __init__(self) -> None:
        timeout = httpx.Timeout(20.0, connect=10.0)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        tmdb = TMDbProvider(self.client)
        omdb = OMDbProvider(self.client)
        jikan = JikanProvider(self.client)
        anilist = AniListProvider(self.client)
        kitsu = KitsuProvider(self.client)

        self._all: list[Provider] = [tmdb, omdb, jikan, anilist, kitsu]
        self._chains: dict[MediaType, list[Provider]] = {
            MediaType.ANIME: [jikan, anilist, kitsu, tmdb, omdb],
            MediaType.SERIES: [tmdb, omdb],
            MediaType.FILM: [tmdb, omdb],
        }

    def chain_for(self, media_type: MediaType) -> list[Provider]:
        return self._chains.get(media_type, [tmdb_first(self._all)])

    async def _cached_search(self, provider: Provider, query: str,
                             media_type: MediaType, year: Optional[int]
                             ) -> Optional[MediaMetadata]:
        cache_key = f"{provider.name}:{media_type.value}:{query.lower()}:{year or 0}"
        cached = await ProviderCacheRepository.get(cache_key)
        if cached is not None:
            return MediaMetadata.from_dict(cached) if cached else None
        result = await provider.search(query, media_type, year)
        await ProviderCacheRepository.set(cache_key, result.to_dict() if result else None)
        return result

    async def resolve(self, query: str, media_type: MediaType,
                      year: Optional[int]) -> ResolveResult:
        """Walk the fallback chain and return the best metadata match."""
        if not query:
            return ResolveResult(None, "", 0.0, False)

        threshold = settings.provider_match_threshold
        best: Optional[MediaMetadata] = None
        best_provider = ""
        best_score = -1.0

        for provider in self.chain_for(media_type):
            if not provider.enabled or not provider.supports(media_type):
                continue
            try:
                meta = await self._cached_search(provider, query, media_type, year)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Provider %s errored on %r: %s", provider.name, query, exc)
                continue
            if meta is None or not meta.title:
                continue
            score = title_similarity(query, meta.title)
            if meta.original_title:
                score = max(score, title_similarity(query, meta.original_title))
            if score >= threshold:
                return ResolveResult(meta, provider.name, score, True)
            if score > best_score:
                best, best_provider, best_score = meta, provider.name, score

        if best is not None:
            log.info("No strong match for %r (best %.0f via %s); keeping partial.",
                     query, best_score, best_provider)
            return ResolveResult(best, best_provider, best_score, False)
        return ResolveResult(None, "", 0.0, False)

    async def aclose(self) -> None:
        await self.client.aclose()


def tmdb_first(providers: list[Provider]) -> Provider:
    for p in providers:
        if p.name == "tmdb":
            return p
    return providers[0]
