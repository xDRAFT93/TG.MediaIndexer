"""Audnexus audiobook provider (primary, now also title-searchable).

Audnexus (https://api.audnex.us) is keyed by Audible ASIN. To make it the first
provider even for plain file names, this provider resolves an ASIN by querying
the regional Audible catalog by keywords (author + title from the file name /
post text), then fetches the rich, normalised metadata from Audnexus for that
ASIN. If an ASIN is already present in the text it is used directly. When the
Audnexus fetch fails the Audible catalog product itself is used as a fallback,
so a title search still yields title / author / narrator / cover / description.
Only if Audible finds nothing does this provider yield to Google Books / DNB /
Open Library.
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from .base import MediaMetadata, Provider

log = get_logger("providers.audnexus")

AUDNEXUS = "https://api.audnex.us"
# Audible ASINs are 10-char, start with B0, uppercase alnum.
ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")

# Audible regional API domains by Audnexus region code.
_AUDIBLE_TLD = {
    "de": "de", "us": "com", "uk": "co.uk", "fr": "fr", "ca": "ca",
    "au": "com.au", "in": "in", "it": "it", "es": "es", "jp": "co.jp",
}
_CATALOG_GROUPS = "contributors,product_desc,product_attrs,media"


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

    def _audible_base(self) -> str:
        tld = _AUDIBLE_TLD.get(settings.audnexus_region, "de")
        return f"https://api.audible.{tld}/1.0/catalog/products"

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if media_type != MediaType.AUDIOBOOK or not query:
            return None

        asin = extract_asin(query)
        catalog_product = None
        if not asin:
            catalog_product = await self._search_audible(query)
            if catalog_product is None:
                return None  # nothing on Audible -> let the next provider try
            asin = catalog_product.get("asin", "")

        # Prefer the rich, normalised Audnexus record for the resolved ASIN.
        if asin:
            meta = await self._fetch_audnexus(asin, query)
            if meta is not None:
                return meta
        # Fallback: build from the Audible catalog product we already fetched.
        if catalog_product is not None:
            return _from_audible_product(catalog_product, query)
        return None

    async def _search_audible(self, query: str) -> Optional[dict]:
        """Top Audible catalog product for a keyword (author+title) search."""
        params = {
            "keywords": query,
            "num_results": 5,
            "products_sort_by": "Relevance",
            "response_groups": _CATALOG_GROUPS,
        }
        try:
            r = await self.client.get(self._audible_base(), params=params)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("Audible catalog search failed for %r: %s", query, exc)
            return None
        products = data.get("products") or []
        return products[0] if products else None

    async def _fetch_audnexus(self, asin: str, query: str) -> Optional[MediaMetadata]:
        url = f"{AUDNEXUS}/books/{asin}"
        try:
            r = await self.client.get(url, params={"region": settings.audnexus_region})
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("Audnexus fetch failed for %s: %s", asin, exc)
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


def _from_audible_product(p: dict, query: str) -> Optional[MediaMetadata]:
    if not p.get("title"):
        return None
    authors = [a.get("name", "") for a in p.get("authors", []) if a.get("name")]
    narrators = [n.get("name", "") for n in p.get("narrators", []) if n.get("name")]
    return MediaMetadata(
        title=p.get("title", "") or query,
        provider="audnexus",
        external_id=p.get("asin", ""),
        original_title=p.get("subtitle", "") or "",
        year=_year(p.get("release_date")),
        overview=_clean(p.get("merchandising_summary") or p.get("publisher_summary") or ""),
        release_date=p.get("release_date", "") or "",
        runtime=_minutes(p.get("runtime_length_min")),
        poster_url=_image(p.get("product_images")),
        authors=authors,
        narrator=", ".join(narrators),
    )


def _image(images) -> str:
    if not isinstance(images, dict) or not images:
        return ""
    # Prefer the largest available square cover.
    for key in ("1024", "900", "750", "500", "256"):
        if images.get(key):
            return str(images[key]).replace("http://", "https://")
    # Otherwise any value.
    val = next(iter(images.values()), "")
    return str(val).replace("http://", "https://") if val else ""


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

