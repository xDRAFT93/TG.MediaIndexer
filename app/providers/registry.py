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
from .audnexus import AudnexusProvider
from .base import MediaMetadata, Provider
from .dnb import DNBProvider
from .googlebooks import GoogleBooksProvider
from .jikan import JikanProvider
from .kitsu import KitsuProvider
from .omdb import OMDbProvider
from .openlibrary import OpenLibraryProvider
from .tmdb import TMDbProvider
from ..config import settings
from ..detection.confidence import title_similarity
from ._bookmatch import audiobook_score
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
        audnexus = AudnexusProvider(self.client)
        googlebooks = GoogleBooksProvider(self.client)
        dnb = DNBProvider(self.client)
        openlibrary = OpenLibraryProvider(self.client)

        self._all: list[Provider] = [tmdb, omdb, jikan, anilist, kitsu,
                                     audnexus, googlebooks, dnb, openlibrary]
        self._chains: dict[MediaType, list[Provider]] = {
            MediaType.ANIME: [jikan, anilist, kitsu, tmdb, omdb],
            MediaType.SERIES: [tmdb, omdb],
            MediaType.FILM: [tmdb, omdb],
            # Audnexus (ASIN) first, then title-searchable book sources, DNB for
            # the German bias before the generic Open Library fallback.
            MediaType.AUDIOBOOK: [audnexus, googlebooks, dnb, openlibrary],
        }

    def chain_for(self, media_type: MediaType) -> list[Provider]:
        return self._chains.get(media_type, [tmdb_first(self._all)])

    async def _cached_search(self, provider: Provider, query: str,
                             media_type: MediaType, year: Optional[int],
                             hints: Optional[dict] = None
                             ) -> Optional[MediaMetadata]:
        # Audiobook results depend on the author/band/asin hints (they pick the
        # ASIN), so fold a small hint signature into the cache key.
        hint_sig = ""
        if media_type == MediaType.AUDIOBOOK and hints:
            authors = ",".join(sorted(a.lower() for a in (hints.get("authors") or [])))
            hint_sig = f":{authors}:{hints.get('volume') or ''}:{(hints.get('asin') or '').upper()}"
        cache_key = f"{provider.name}:{media_type.value}:{query.lower()}:{year or 0}{hint_sig}"
        cached = await ProviderCacheRepository.get(cache_key)
        if cached is not None:
            return MediaMetadata.from_dict(cached) if cached else None
        result = await provider.search(query, media_type, year, hints)
        await ProviderCacheRepository.set(cache_key, result.to_dict() if result else None)
        return result

    async def resolve(self, query: str, media_type: MediaType,
                      year: Optional[int],
                      hints: Optional[dict] = None) -> ResolveResult:
        """Walk the fallback chain and return the best metadata match.

        ``hints`` (audiobooks): {authors, narrator, series, volume, language,
        asin}. They drive the composite score and a strict accept gate so a
        wrong ASIN is never stored, and let a stored ASIN re-fetch directly from
        Audnexus (the primary source).
        """
        if not query:
            return ResolveResult(None, "", 0.0, False)

        threshold = settings.provider_match_threshold
        if media_type == MediaType.ANIME:
            threshold = settings.anime_match_threshold
        elif media_type == MediaType.AUDIOBOOK:
            threshold = settings.audiobook_match_threshold
        best: Optional[MediaMetadata] = None
        best_provider = ""
        best_score = -1.0
        h_authors = (hints or {}).get("authors") or []

        for provider in self.chain_for(media_type):
            if not provider.enabled or not provider.supports(media_type):
                continue
            try:
                meta = await self._cached_search(provider, query, media_type, year, hints)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Provider %s errored on %r: %s", provider.name, query, exc)
                continue
            if meta is None or not meta.title:
                continue
            if media_type == MediaType.AUDIOBOOK:
                score, title_score, a_ov = audiobook_score(query, meta, hints)
                # Strict accept: title must clear the threshold AND, when authors
                # are known, at least one author token must match. This is what
                # prevents storing a wrong book / wrong ASIN.
                accept = title_score >= threshold and (not h_authors or a_ov >= 0.34)
                if accept:
                    result = ResolveResult(meta, provider.name, score, True)
                    return await self._enrich_audiobook(result, query, year, hints)
                if score > best_score:
                    best, best_provider, best_score = meta, provider.name, score
                continue

            score = title_similarity(query, meta.title)
            if meta.original_title:
                score = max(score, title_similarity(query, meta.original_title))
            if score >= threshold:
                return ResolveResult(meta, provider.name, score, True)
            if score > best_score:
                best, best_provider, best_score = meta, provider.name, score

        # For anime AND audiobooks a weak "best" guess is more harmful than no
        # data at all (short anime titles collide; books collide on author), so a
        # below-threshold best is discarded and the entry stays unresolved. For
        # film/series a partial best is kept (unresolved) as a hint.
        strict = media_type in (MediaType.ANIME, MediaType.AUDIOBOOK)
        if best is not None and not strict:
            log.info("No strong match for %r (best %.0f via %s); keeping partial.",
                     query, best_score, best_provider)
            return ResolveResult(best, best_provider, best_score, False)
        if best is not None:
            log.info("%r below strict threshold (best %.0f via %s) — discarding.",
                     query, best_score, best_provider)
        return ResolveResult(None, "", 0.0, False)

    async def _enrich_audiobook(self, result: ResolveResult, query: str,
                                year: Optional[int],
                                hints: Optional[dict]) -> ResolveResult:
        """Audnexus/Audible stays the authoritative source; the other book
        providers only FILL gaps (overview, cover, genres) and never override the
        ASIN/title/authors. Used only for an accepted audiobook match."""
        meta = result.metadata
        if meta is None or result.provider != "audnexus":
            return result
        if meta.overview and meta.poster_url and meta.genres:
            return result  # already complete
        for provider in self.chain_for(MediaType.AUDIOBOOK):
            if provider.name == "audnexus" or not provider.enabled:
                continue
            if meta.overview and meta.poster_url and meta.genres:
                break
            try:
                extra = await self._cached_search(provider, query,
                                                  MediaType.AUDIOBOOK, year, hints)
            except Exception:  # pragma: no cover - defensive
                continue
            if extra is None:
                continue
            # Verification: only enrich from a source that agrees on the title.
            if title_similarity(meta.title, extra.title) < 80:
                continue
            if not meta.overview and extra.overview:
                meta.overview = extra.overview
            if not meta.poster_url and extra.poster_url:
                meta.poster_url = extra.poster_url
            if not meta.genres and extra.genres:
                meta.genres = list(extra.genres)
        return result

    async def aclose(self) -> None:
        await self.client.aclose()


def tmdb_first(providers: list[Provider]) -> Provider:
    for p in providers:
        if p.name == "tmdb":
            return p
    return providers[0]
