"""Domain models.

Plain dataclasses with explicit (de)serialisation to/from MongoDB documents.
The database is the single source of truth; these objects are transient views.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from ..util import new_id, now_utc, slugify


class MediaType(str, Enum):
    FILM = "film"
    SERIES = "series"
    ANIME = "anime"
    AUDIOBOOK = "audiobook"

    @classmethod
    def coerce(cls, value: Any) -> "MediaType":
        if isinstance(value, MediaType):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.FILM


class PostState(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    ARCHIVED = "ARCHIVED"


class EventStage(str, Enum):
    INGESTED = "ingested"
    PROCESSING = "processing"
    PROCESSED = "processed"
    PENDING = "pending"
    ERROR = "error"
    IGNORED = "ignored"
    CONTEXT = "context"      # no file: only set the thread's provisional title


def media_canonical_key(media_type: MediaType, title: str, year: Optional[int]) -> str:
    """Deterministic dedup key. Same key => same media => merge."""
    return f"{media_type.value}:{slugify(title)}:{year or 0}"


def episode_canonical_key(media_id: str, season: int, episode: int) -> str:
    return f"{media_id}:s{season:02d}e{episode:03d}"


# --------------------------------------------------------------------------- #
# Releases (a concrete file/version of a media or an episode)
# --------------------------------------------------------------------------- #
@dataclass
class Release:
    file_name: str = ""
    quality: str = ""           # e.g. 1080p
    source_tag: str = ""        # e.g. BluRay, WEB-DL
    codec: str = ""             # e.g. x265
    group: str = ""             # release group
    language: str = ""
    size_bytes: Optional[int] = None
    chat_id: Optional[int] = None
    thread_id: Optional[int] = None
    message_id: Optional[int] = None
    added_at: datetime = field(default_factory=now_utc)

    def dedup_key(self) -> str:
        # file_name is the unchangeable raw key; fall back to message id.
        return self.file_name.strip().lower() or f"msg:{self.message_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Release":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Episode
# --------------------------------------------------------------------------- #
@dataclass
class Episode:
    media_id: str
    season: int = 1
    episode: int = 1
    title: str = ""
    overview: str = ""
    air_date: str = ""
    releases: list[Release] = field(default_factory=list)
    _id: str = field(default_factory=new_id)
    canonical_key: str = ""
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.canonical_key:
            self.canonical_key = episode_canonical_key(self.media_id, self.season, self.episode)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["releases"] = [r if isinstance(r, dict) else r.to_dict() for r in self.releases]  # type: ignore
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        releases = [Release.from_dict(r) for r in d.get("releases", [])]
        return cls(
            media_id=d["media_id"],
            season=d.get("season", 1),
            episode=d.get("episode", 1),
            title=d.get("title", ""),
            overview=d.get("overview", ""),
            air_date=d.get("air_date", ""),
            releases=releases,
            _id=d.get("_id", new_id()),
            canonical_key=d.get("canonical_key", ""),
            created_at=d.get("created_at", now_utc()),
            updated_at=d.get("updated_at", now_utc()),
        )


# --------------------------------------------------------------------------- #
# Media source reference
# --------------------------------------------------------------------------- #
@dataclass
class SourceRef:
    chat_id: int
    thread_id: Optional[int] = None
    first_message_id: Optional[int] = None
    last_message_id: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Media (the wiki entry)
# --------------------------------------------------------------------------- #
@dataclass
class Media:
    media_type: MediaType
    title: str
    year: Optional[int] = None
    original_title: str = ""
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    rating: Optional[float] = None
    votes: Optional[int] = None
    release_date: str = ""
    runtime: Optional[int] = None
    poster_url: str = ""
    # Audiobook-specific (empty for film/series/anime).
    authors: list[str] = field(default_factory=list)
    narrator: str = ""
    tags: list[str] = field(default_factory=list)
    providers: dict[str, str] = field(default_factory=dict)   # provider -> external id
    provider_used: str = ""
    releases: list[Release] = field(default_factory=list)     # for films
    sources: list[dict] = field(default_factory=list)         # SourceRef dicts
    _id: str = field(default_factory=new_id)
    canonical_key: str = ""
    root_post_id: str = ""
    ui_dirty: bool = True
    metadata_resolved: bool = False
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        self.media_type = MediaType.coerce(self.media_type)
        if not self.canonical_key:
            self.canonical_key = media_canonical_key(self.media_type, self.title, self.year)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["media_type"] = self.media_type.value
        d["releases"] = [r if isinstance(r, dict) else r.to_dict() for r in self.releases]  # type: ignore
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Media":
        return cls(
            media_type=MediaType.coerce(d.get("media_type")),
            title=d.get("title", ""),
            year=d.get("year"),
            original_title=d.get("original_title", ""),
            overview=d.get("overview", ""),
            genres=list(d.get("genres", [])),
            rating=d.get("rating"),
            votes=d.get("votes"),
            release_date=d.get("release_date", ""),
            runtime=d.get("runtime"),
            poster_url=d.get("poster_url", ""),
            authors=list(d.get("authors", []) or []),
            narrator=d.get("narrator", "") or "",
            tags=list(d.get("tags", [])),
            providers=dict(d.get("providers", {})),
            provider_used=d.get("provider_used", ""),
            releases=[Release.from_dict(r) for r in d.get("releases", [])],
            sources=list(d.get("sources", [])),
            _id=d.get("_id", new_id()),
            canonical_key=d.get("canonical_key", ""),
            root_post_id=d.get("root_post_id", ""),
            ui_dirty=d.get("ui_dirty", True),
            metadata_resolved=d.get("metadata_resolved", False),
            created_at=d.get("created_at", now_utc()),
            updated_at=d.get("updated_at", now_utc()),
        )


# --------------------------------------------------------------------------- #
# Raw event captured from Telegram
# --------------------------------------------------------------------------- #
@dataclass
class RawEvent:
    chat_id: int
    message_id: int
    thread_id: Optional[int] = None
    message_text: str = ""
    caption: str = ""
    file_name: str = ""          # ONLY message.document/video file_name
    media_type_raw: str = ""     # telegram media kind: document/video/none
    mime_type: str = ""
    size_bytes: Optional[int] = None
    sender_id: Optional[int] = None
    is_bot: bool = False
    timestamp: Optional[datetime] = None
    _id: str = field(default_factory=new_id)
    stage: str = EventStage.INGESTED.value
    classification: dict = field(default_factory=dict)
    error: str = ""
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RawEvent":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Per-thread context (persistent, never RAM-only)
# --------------------------------------------------------------------------- #
@dataclass
class ThreadState:
    chat_id: int
    thread_id: Optional[int]
    active_media_id: str = ""
    active_title: str = ""
    active_media_type: str = ""
    active_resolved: bool = False   # whether the active media has provider metadata
    # Provisional context from a file-less announcement (image + title) that may
    # not yet have a media entry. The first real file in the thread uses it to
    # create the single correct entry and bind to it.
    pending_title: str = ""
    pending_type: str = ""
    episode_cursor: int = 0       # last sequential episode number assigned
    season_cursor: int = 1
    last_event_id: str = ""
    updated_at: datetime = field(default_factory=now_utc)

    @property
    def key(self) -> str:
        return f"{self.chat_id}:{self.thread_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_id"] = self.key
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ThreadState":
        return cls(
            chat_id=d["chat_id"],
            thread_id=d.get("thread_id"),
            active_media_id=d.get("active_media_id", ""),
            active_title=d.get("active_title", ""),
            active_media_type=d.get("active_media_type", ""),
            active_resolved=d.get("active_resolved", False),
            pending_title=d.get("pending_title", ""),
            pending_type=d.get("pending_type", ""),
            episode_cursor=d.get("episode_cursor", 0),
            season_cursor=d.get("season_cursor", 1),
            last_event_id=d.get("last_event_id", ""),
            updated_at=d.get("updated_at", now_utc()),
        )


# --------------------------------------------------------------------------- #
# UI Post (root or overflow)
# --------------------------------------------------------------------------- #
@dataclass
class Post:
    media_id: str
    chat_id: int
    topic_id: Optional[int]
    message_id: int
    role: str = "root"            # root | overflow
    part_index: int = 0
    parent_post_id: str = ""
    has_media: bool = False       # True when this post is a photo (poster) message
    state: str = PostState.CREATED.value
    content_hash: str = ""
    char_len: int = 0
    _id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[attr-defined]
