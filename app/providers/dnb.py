"""Deutsche Nationalbibliothek provider (SRU / Dublin Core).

The DNB exposes an SRU endpoint with authoritative German bibliographic data.
We request the simple ``oai_dc`` (Dublin Core) schema, which is far easier to
parse than MARC21 while carrying everything we need: title, creator (author),
date and description. Ideal as the German-focused fallback behind Audnexus /
Google Books.
"""
from __future__ import annotations

import re
from typing import Optional
from xml.etree import ElementTree as ET

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import MediaType
from ._bookmatch import select_best
from .base import MediaMetadata, Provider

log = get_logger("providers.dnb")

BASE = "https://services.dnb.de/sru/dnb"
_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


class DNBProvider(Provider):
    name = "dnb"

    def supports(self, media_type: MediaType) -> bool:
        return media_type == MediaType.AUDIOBOOK

    @property
    def enabled(self) -> bool:
        return True  # no key required

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        if media_type != MediaType.AUDIOBOOK or not query:
            return None
        params = {
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": f'WOE="{query}"',
            "maximumRecords": "5",
            "recordSchema": "oai_dc",
        }
        try:
            r = await self.client.get(BASE, params=params)
            if r.status_code != 200:
                return None
            root = ET.fromstring(r.text)
        except Exception as exc:  # pragma: no cover - network / parse
            log.warning("DNB failed for %r: %s", query, exc)
            return None

        return parse_dnb(root, query)


def parse_dnb(root: ET.Element, query: str) -> Optional[MediaMetadata]:
    """Extract the best-matching Dublin Core record. Kept pure for testing."""
    records = root.findall(".//oai_dc:dc", _NS)
    if not records:
        records = root.findall(".//srw:recordData", _NS)
    if not records:
        return None

    def _vals(record, tag: str) -> list[str]:
        return [e.text.strip() for e in record.findall(f"dc:{tag}", _NS)
                if e is not None and e.text and e.text.strip()]

    parsed = []
    for rec in records:
        titles = _vals(rec, "title")
        if not titles:
            continue
        parsed.append({
            "title": titles[0],
            "original_title": "",
            "authors": _vals(rec, "creator") or _vals(rec, "contributor"),
            "dates": _vals(rec, "date"),
            "descriptions": _vals(rec, "description"),
            "subjects": _vals(rec, "subject"),
            "identifier": next(iter(_vals(rec, "identifier")), ""),
        })
    if not parsed:
        return None

    best, score = select_best(query, parsed)
    if best is None or score < 50:
        return None
    dates = best["dates"]
    return MediaMetadata(
        title=best["title"],
        provider="dnb",
        external_id=best["identifier"],
        year=_year(dates[0] if dates else None),
        overview=best["descriptions"][0] if best["descriptions"] else "",
        genres=best["subjects"][:5],
        release_date=dates[0] if dates else "",
        authors=best["authors"],
    )


def _year(value) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None
