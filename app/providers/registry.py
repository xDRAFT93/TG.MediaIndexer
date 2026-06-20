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
            # Audnexus/Audible is the ONLY data source for audiobooks (text AND
            # cover). The book catalogues below never supply postable data.
            MediaType.AUDIOBOOK: [audnexus],
        }
        # Identification-only helpers: used solely to find a cleaner title/author
        # so Audible can be re-searched. Their metadata is never returned/posted.
        self._book_identifiers: list[Provider] = [googlebooks, dnb, openlibrary]

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

        if media_type == MediaType.AUDIOBOOK:
            return await self._resolve_audiobook(query, year, hints)

        threshold = settings.provider_match_threshold
        if media_type == MediaType.ANIME:
            threshold = settings.anime_match_threshold
        best: Optional[MediaMetadata] = None
        best_provider = ""
        best_score = -1.0

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
            score = title_similarity(query, meta.title)
            if meta.original_title:
                score = max(score, title_similarity(query, meta.original_title))
            if score >= threshold:
                return ResolveResult(meta, provider.name, score, True)
            if score > best_score:
                best, best_provider, best_score = meta, provider.name, score

        # For anime a weak "best" guess is more harmful than no data, so a
        # below-threshold best is discarded. For film/series a partial best is
        # kept (unresolved) as a hint.
        if best is not None and media_type != MediaType.ANIME:
            log.info("No strong match for %r (best %.0f via %s); keeping partial.",
                     query, best_score, best_provider)
            return ResolveResult(best, best_provider, best_score, False)
        if best is not None:
            log.info("%r below strict threshold (best %.0f via %s) — discarding.",
                     query, best_score, best_provider)
        return ResolveResult(None, "", 0.0, False)

    async def _resolve_audiobook(self, query: str, year: Optional[int],
                                 hints: Optional[dict]) -> ResolveResult:
        """Resolve an audiobook with Audnexus/Audible as the SOLE data source.

        Google Books / DNB / Open Library are consulted only to obtain a cleaner
        title+author with which Audible is searched again — their metadata is
        never returned, so no foreign data (or cover) is ever posted. Nothing is
        accepted unless Audible/Audnexus confirms it with a strong title match
        and (when authors are known) a matching author, so no wrong ASIN sticks.
        """
        threshold = settings.audiobook_match_threshold
        h_authors = (hints or {}).get("authors") or []
        audnexus = self._provider("audnexus")
        if audnexus is None:
            return ResolveResult(None, "", 0.0, False)

        best_partial: Optional[tuple[MediaMetadata, float]] = None

        async def try_audnexus(q: str, h: Optional[dict]) -> Optional[ResolveResult]:
            nonlocal best_partial
            try:
                meta = await self._cached_search(audnexus, q, MediaType.AUDIOBOOK, year, h)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Audnexus errored on %r: %s", q, exc)
                return None
            if meta is None or not meta.title:
                return None
            score, title_score, a_ov = audiobook_score(q, meta, h)
            ha = (h or {}).get("authors") or []
            if title_score >= threshold and (not ha or a_ov >= 0.34):
                return ResolveResult(meta, "audnexus", score, True)
            if best_partial is None or score > best_partial[1]:
                best_partial = (meta, score)
            return None

        # 1) Direct Audible/Audnexus search with the detected query.
        hit = await try_audnexus(query, hints)
        if hit:
            return hit

        # 2) Identification pass: ask the book catalogues for a canonical
        #    title/author, then re-search Audible with that refined query.
        seen = {query.lower()}
        for idp in self._book_identifiers:
            if not idp.enabled or not idp.supports(MediaType.AUDIOBOOK):
                continue
            try:
                ident = await self._cached_search(idp, query, MediaType.AUDIOBOOK, year, hints)
            except Exception:  # pragma: no cover - defensive
                continue
            if ident is None or not ident.title:
                continue
            authors = h_authors or list(getattr(ident, "authors", []) or [])
            refined_variants = []
            if authors:
                refined_variants.append(f"{' '.join(authors)} {ident.title}")
            refined_variants.append(ident.title)
            ref_hints = {**(hints or {}), "authors": authors}
            for rq in refined_variants:
                if rq.lower() in seen:
                    continue
                seen.add(rq.lower())
                hit = await try_audnexus(rq, ref_hints)
                if hit:
                    log.info("Audiobook resolved via %s identification -> Audible %r.",
                             idp.name, rq)
                    return hit

        log.info("Audiobook %r: no confident Audible match — left unresolved.", query)
        return ResolveResult(None, "", 0.0, False)

    def _provider(self, name: str) -> Optional[Provider]:
        for p in self._all:
            if p.name == name:
                return p
        return None

    async def aclose(self) -> None:
        await self.client.aclose()


def tmdb_first(providers: list[Provider]) -> Provider:
    for p in providers:
        if p.name == "tmdb":
            return p
    return providers[0]
