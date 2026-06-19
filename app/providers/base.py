"""Provider interface and the normalised metadata returned by every provider."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..storage.models import MediaType


@dataclass
class MediaMetadata:
    title: str
    provider: str
    external_id: str = ""
    original_title: str = ""
    year: Optional[int] = None
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    rating: Optional[float] = None
    votes: Optional[int] = None
    release_date: str = ""
    runtime: Optional[int] = None
    poster_url: str = ""
    authors: list[str] = field(default_factory=list)
    narrator: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaMetadata":
        # Only pass keys that are present so newly added fields fall back to their
        # defaults for older cached entries instead of becoming None.
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})  # type: ignore[attr-defined]


class Provider:
    """Base class for metadata providers."""

    name: str = "base"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def supports(self, media_type: MediaType) -> bool:  # pragma: no cover - overridden
        return True

    @property
    def enabled(self) -> bool:  # pragma: no cover - overridden
        return True

    async def search(self, query: str, media_type: MediaType,
                     year: Optional[int]) -> Optional[MediaMetadata]:
        raise NotImplementedError
