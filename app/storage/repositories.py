"""Repositories: the only place that touches the database directly.

Implements the hard de-duplication rules:
  * same media canonical_key  -> merged (never duplicated)
  * same episode canonical_key-> updated/merged
  * same release file_name    -> merged
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..config import settings
from ..util import now_utc
from .database import db
from .models import (
    Episode,
    EventStage,
    Media,
    Post,
    RawEvent,
    Release,
    ThreadState,
)


# --------------------------------------------------------------------------- #
# Merge helpers
# --------------------------------------------------------------------------- #
def _merge_lists(a: list, b: list) -> list:
    out = list(a)
    seen = {str(x).lower() for x in a}
    for item in b:
        if str(item).lower() not in seen:
            out.append(item)
            seen.add(str(item).lower())
    return out


def _merge_releases(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for r in existing + incoming:
        rel = Release.from_dict(r) if not isinstance(r, Release) else r
        key = rel.dedup_key()
        if key not in by_key:
            by_key[key] = rel.to_dict()
        else:
            # Keep richest known fields.
            merged = by_key[key]
            for f, v in rel.to_dict().items():
                if v and not merged.get(f):
                    merged[f] = v
    return list(by_key.values())


def _prefer(existing_val, incoming_val):
    """Keep existing unless it is empty/None, then take incoming."""
    if existing_val in (None, "", [], {}):
        return incoming_val
    return existing_val


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #
class MediaRepository:
    coll = "media"

    @classmethod
    async def get(cls, media_id: str) -> Optional[Media]:
        doc = await db()[cls.coll].find_one({"_id": media_id})
        return Media.from_dict(doc) if doc else None

    @classmethod
    async def get_by_key(cls, canonical_key: str) -> Optional[Media]:
        doc = await db()[cls.coll].find_one({"canonical_key": canonical_key})
        return Media.from_dict(doc) if doc else None

    @classmethod
    async def upsert_merge(cls, media: Media) -> Media:
        """Insert new media or merge into the existing one with same key."""
        existing = await cls.get_by_key(media.canonical_key)
        if existing is None:
            media.updated_at = now_utc()
            media.ui_dirty = True
            await db()[cls.coll].insert_one(media.to_dict())
            return media

        e = existing.to_dict()
        n = media.to_dict()
        merged = dict(e)
        # Scalar metadata: keep existing, fill gaps. If incoming is freshly
        # resolved and existing was not, prefer the resolved values.
        prefer_incoming = n.get("metadata_resolved") and not e.get("metadata_resolved")
        for field_name in (
            "title", "original_title", "overview", "release_date",
            "poster_url", "rating", "votes", "runtime", "year", "provider_used",
            "narrator",
        ):
            if prefer_incoming and n.get(field_name) not in (None, "", []):
                merged[field_name] = n[field_name]
            else:
                merged[field_name] = _prefer(e.get(field_name), n.get(field_name))
        merged["genres"] = _merge_lists(e.get("genres", []), n.get("genres", []))
        merged["tags"] = _merge_lists(e.get("tags", []), n.get("tags", []))
        # Authors: prefer freshly resolved, else keep whichever is non-empty.
        if prefer_incoming and n.get("authors"):
            merged["authors"] = n["authors"]
        else:
            merged["authors"] = e.get("authors") or n.get("authors", [])
        # Search aliases accumulate so .repair has every candidate to try.
        merged["search_aliases"] = _merge_lists(
            e.get("search_aliases", []), n.get("search_aliases", []))
        merged["providers"] = {**n.get("providers", {}), **e.get("providers", {})}
        if prefer_incoming:
            merged["providers"] = {**e.get("providers", {}), **n.get("providers", {})}
        merged["releases"] = _merge_releases(e.get("releases", []), n.get("releases", []))
        merged["sources"] = _merge_sources(e.get("sources", []), n.get("sources", []))
        merged["metadata_resolved"] = e.get("metadata_resolved") or n.get("metadata_resolved")
        merged["updated_at"] = now_utc()
        merged["ui_dirty"] = True
        await db()[cls.coll].replace_one({"_id": existing._id}, merged)
        return Media.from_dict(merged)

    @classmethod
    async def add_source(cls, media_id: str, source: dict) -> None:
        media = await cls.get(media_id)
        if not media:
            return
        merged = _merge_sources(media.sources, [source])
        await db()[cls.coll].update_one(
            {"_id": media_id},
            {"$set": {"sources": merged, "updated_at": now_utc()}},
        )

    @classmethod
    async def add_film_release(cls, media_id: str, release: Release) -> None:
        media = await cls.get(media_id)
        if not media:
            return
        merged = _merge_releases([r.to_dict() for r in media.releases], [release.to_dict()])
        await db()[cls.coll].update_one(
            {"_id": media_id},
            {"$set": {"releases": merged, "updated_at": now_utc(), "ui_dirty": True}},
        )

    @classmethod
    async def set_metadata_resolved(cls, media_id: str, resolved: bool) -> None:
        await db()[cls.coll].update_one(
            {"_id": media_id}, {"$set": {"metadata_resolved": resolved}}
        )

    @classmethod
    async def mark_clean(cls, media_id: str) -> None:
        await db()[cls.coll].update_one({"_id": media_id}, {"$set": {"ui_dirty": False}})

    @classmethod
    async def mark_dirty(cls, media_id: str) -> None:
        await db()[cls.coll].update_one(
            {"_id": media_id}, {"$set": {"ui_dirty": True, "updated_at": now_utc()}}
        )

    @classmethod
    async def find_dirty(cls, limit: int = 200) -> list[Media]:
        cur = db()[cls.coll].find({"ui_dirty": True}).limit(limit)
        return [Media.from_dict(d) async for d in cur]

    @classmethod
    async def find_unresolved(cls, limit: int = 200) -> list[Media]:
        cur = db()[cls.coll].find({"metadata_resolved": {"$ne": True}}).limit(limit)
        return [Media.from_dict(d) async for d in cur]

    @classmethod
    async def count(cls) -> int:
        return await db()[cls.coll].count_documents({})

    @classmethod
    async def all_ids(cls) -> list[str]:
        """Every media id, for full-catalog operations (.reindex / .prune)."""
        cur = db()[cls.coll].find({}, {"_id": 1})
        return [d["_id"] async for d in cur]

    @classmethod
    async def set_sources(cls, media_id: str, sources: list[dict]) -> None:
        await db()[cls.coll].update_one(
            {"_id": media_id},
            {"$set": {"sources": sources, "ui_dirty": True, "updated_at": now_utc()}},
        )

    @classmethod
    async def set_film_releases(cls, media_id: str, releases: list[dict]) -> None:
        await db()[cls.coll].update_one(
            {"_id": media_id},
            {"$set": {"releases": releases, "ui_dirty": True, "updated_at": now_utc()}},
        )

    @classmethod
    async def delete(cls, media_id: str) -> None:
        await db()[cls.coll].delete_one({"_id": media_id})

    @classmethod
    async def apply_metadata(cls, media_id: str, fields: dict) -> None:
        """Directly overwrite the given metadata fields (no merge). Used by
        audiobook re-verification to replace a wrong match or clear it."""
        payload = {**fields, "updated_at": now_utc(), "ui_dirty": True}
        await db()[cls.coll].update_one({"_id": media_id}, {"$set": payload})

    @classmethod
    async def set_root_post(cls, media_id: str, post_id: str) -> None:
        await db()[cls.coll].update_one({"_id": media_id}, {"$set": {"root_post_id": post_id}})

    @classmethod
    async def search_text(cls, query: str, limit: int = 10) -> list[Media]:
        """Loose title search for owner commands (case-insensitive substring)."""
        import re
        pattern = re.escape(query.strip())
        cur = db()[cls.coll].find(
            {"title": {"$regex": pattern, "$options": "i"}}
        ).limit(limit)
        return [Media.from_dict(d) async for d in cur]


def _merge_sources(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for s in existing + incoming:
        key = (s.get("chat_id"), s.get("thread_id"))
        if key not in by_key:
            by_key[key] = dict(s)
        else:
            cur = by_key[key]
            fmi, lmi = s.get("first_message_id"), s.get("last_message_id")
            if fmi and (not cur.get("first_message_id") or fmi < cur["first_message_id"]):
                cur["first_message_id"] = fmi
            if lmi and (not cur.get("last_message_id") or lmi > cur["last_message_id"]):
                cur["last_message_id"] = lmi
    return list(by_key.values())


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #
class EpisodeRepository:
    coll = "episodes"

    @classmethod
    async def upsert_merge(cls, ep: Episode) -> Episode:
        existing = await db()[cls.coll].find_one({"canonical_key": ep.canonical_key})
        if existing is None:
            ep.updated_at = now_utc()
            await db()[cls.coll].insert_one(ep.to_dict())
            return ep
        e = dict(existing)
        n = ep.to_dict()
        e["title"] = _prefer(e.get("title"), n.get("title"))
        e["overview"] = _prefer(e.get("overview"), n.get("overview"))
        e["air_date"] = _prefer(e.get("air_date"), n.get("air_date"))
        e["releases"] = _merge_releases(e.get("releases", []), n.get("releases", []))
        e["updated_at"] = now_utc()
        await db()[cls.coll].replace_one({"_id": e["_id"]}, e)
        return Episode.from_dict(e)

    @classmethod
    async def list_for_media(cls, media_id: str) -> list[Episode]:
        cur = db()[cls.coll].find({"media_id": media_id}).sort(
            [("season", 1), ("episode", 1)]
        )
        return [Episode.from_dict(d) async for d in cur]

    @classmethod
    async def count_for_media(cls, media_id: str) -> int:
        return await db()[cls.coll].count_documents({"media_id": media_id})

    @classmethod
    async def reassign(cls, episode_id: str, new_media_id: str, season: int, episode: int) -> None:
        from .models import episode_canonical_key
        await db()[cls.coll].update_one(
            {"_id": episode_id},
            {"$set": {
                "media_id": new_media_id,
                "canonical_key": episode_canonical_key(new_media_id, season, episode),
                "updated_at": now_utc(),
            }},
        )

    @classmethod
    async def set_releases(cls, episode_id: str, releases: list[dict]) -> None:
        await db()[cls.coll].update_one(
            {"_id": episode_id},
            {"$set": {"releases": releases, "updated_at": now_utc()}},
        )

    @classmethod
    async def delete(cls, episode_id: str) -> None:
        await db()[cls.coll].delete_one({"_id": episode_id})


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
class EventRepository:
    coll = "events"

    @classmethod
    async def insert_if_new(cls, event: RawEvent) -> tuple[RawEvent, bool]:
        """Returns (event, created). Idempotent on (chat_id, message_id)."""
        existing = await db()[cls.coll].find_one(
            {"chat_id": event.chat_id, "message_id": event.message_id}
        )
        if existing:
            return RawEvent.from_dict(existing), False
        await db()[cls.coll].insert_one(event.to_dict())
        return event, True

    @classmethod
    async def get(cls, event_id: str) -> Optional[RawEvent]:
        doc = await db()[cls.coll].find_one({"_id": event_id})
        return RawEvent.from_dict(doc) if doc else None

    @classmethod
    async def set_stage(cls, event_id: str, stage: str, *, error: str = "",
                        classification: Optional[dict] = None) -> None:
        update = {"stage": stage, "updated_at": now_utc()}
        if error:
            update["error"] = error
        if classification is not None:
            update["classification"] = classification
        await db()[cls.coll].update_one({"_id": event_id}, {"$set": update})

    @classmethod
    async def find_resumable(cls) -> list[RawEvent]:
        cur = db()[cls.coll].find(
            {"stage": {"$in": [EventStage.INGESTED.value, EventStage.PROCESSING.value]}}
        ).sort("created_at", 1)
        return [RawEvent.from_dict(d) async for d in cur]

    @classmethod
    async def recent(cls, limit: Optional[int] = None) -> list[RawEvent]:
        limit = limit or settings.recent_events_keep
        cur = db()[cls.coll].find().sort("created_at", -1).limit(limit)
        return [RawEvent.from_dict(d) async for d in cur]

    @classmethod
    async def trim_recent(cls) -> None:
        """Keep storage small: delete processed/ignored events beyond the
        configured recent window (pending/error are always kept)."""
        keep = settings.recent_events_keep
        keepable = {EventStage.PROCESSED.value, EventStage.IGNORED.value}
        docs = db()[cls.coll].find(
            {"stage": {"$in": list(keepable)}}
        ).sort("created_at", -1).skip(keep)
        ids = [d["_id"] async for d in docs]
        if ids:
            await db()[cls.coll].delete_many({"_id": {"$in": ids}})


# --------------------------------------------------------------------------- #
# Pending events (unclassified, to be retried by healer)
# --------------------------------------------------------------------------- #
class PendingRepository:
    coll = "pending_events"

    @classmethod
    async def add(cls, event: RawEvent, reason: str) -> None:
        await db()[cls.coll].update_one(
            {"chat_id": event.chat_id, "message_id": event.message_id},
            {
                "$set": {
                    "event_id": event._id,
                    "chat_id": event.chat_id,
                    "thread_id": event.thread_id,
                    "message_id": event.message_id,
                    "reason": reason,
                    "snapshot": event.to_dict(),
                    "updated_at": now_utc(),
                },
                "$setOnInsert": {"created_at": now_utc(), "attempts": 0},
            },
            upsert=True,
        )

    @classmethod
    async def list(cls, limit: int = 500) -> list[dict]:
        cur = db()[cls.coll].find().sort("created_at", 1).limit(limit)
        return [d async for d in cur]

    @classmethod
    async def increment_attempt(cls, chat_id: int, message_id: int) -> int:
        doc = await db()[cls.coll].find_one_and_update(
            {"chat_id": chat_id, "message_id": message_id},
            {"$inc": {"attempts": 1}, "$set": {"updated_at": now_utc()}},
            return_document=True,
        )
        return doc.get("attempts", 0) if doc else 0

    @classmethod
    async def remove(cls, chat_id: int, message_id: int) -> None:
        await db()[cls.coll].delete_one({"chat_id": chat_id, "message_id": message_id})

    @classmethod
    async def count(cls) -> int:
        return await db()[cls.coll].count_documents({})


# --------------------------------------------------------------------------- #
# Thread state
# --------------------------------------------------------------------------- #
class ThreadStateRepository:
    coll = "thread_state"

    @classmethod
    async def get_or_create(cls, chat_id: int, thread_id: Optional[int]) -> ThreadState:
        doc = await db()[cls.coll].find_one({"chat_id": chat_id, "thread_id": thread_id})
        if doc:
            return ThreadState.from_dict(doc)
        st = ThreadState(chat_id=chat_id, thread_id=thread_id)
        await db()[cls.coll].insert_one(st.to_dict())
        return st

    @classmethod
    async def save(cls, st: ThreadState) -> None:
        st.updated_at = now_utc()
        await db()[cls.coll].replace_one({"_id": st.key}, st.to_dict(), upsert=True)


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #
class PostRepository:
    coll = "posts"

    @classmethod
    async def list_for_media(cls, media_id: str) -> list[Post]:
        cur = db()[cls.coll].find({"media_id": media_id}).sort("part_index", 1)
        return [Post.from_dict(d) async for d in cur]

    @classmethod
    async def insert(cls, post: Post) -> Post:
        await db()[cls.coll].insert_one(post.to_dict())
        return post

    @classmethod
    async def update(cls, post: Post) -> None:
        post.updated_at = now_utc()
        await db()[cls.coll].replace_one({"_id": post._id}, post.to_dict())

    @classmethod
    async def delete(cls, post_id: str) -> None:
        await db()[cls.coll].delete_one({"_id": post_id})

    @classmethod
    async def count(cls) -> int:
        return await db()[cls.coll].count_documents({})


# --------------------------------------------------------------------------- #
# Provider cache (avoid hammering external APIs / speed up imports)
# --------------------------------------------------------------------------- #
class ProviderCacheRepository:
    coll = "provider_cache"

    @classmethod
    async def get(cls, key: str) -> Optional[dict]:
        doc = await db()[cls.coll].find_one({"key": key})
        if not doc:
            return None
        ttl = timedelta(days=settings.provider_cache_ttl_days)
        if now_utc() - doc.get("created_at", now_utc()) > ttl:
            await db()[cls.coll].delete_one({"key": key})
            return None
        return doc.get("payload")

    @classmethod
    async def set(cls, key: str, payload: Optional[dict]) -> None:
        await db()[cls.coll].update_one(
            {"key": key},
            {"$set": {"key": key, "payload": payload, "created_at": now_utc()}},
            upsert=True,
        )


# --------------------------------------------------------------------------- #
# Pattern / alias tables (explicit, structured "learning")
# --------------------------------------------------------------------------- #
class PatternRepository:
    coll = "patterns"

    @classmethod
    async def add_alias(cls, normalized_alias: str, media_id: str,
                        media_type: str = "", confidence: float = 1.0) -> None:
        await db()[cls.coll].update_one(
            {"type": "alias", "key": normalized_alias},
            {"$set": {
                "type": "alias", "key": normalized_alias, "value": media_id,
                "media_type": media_type, "confidence": confidence,
                "updated_at": now_utc(),
            }},
            upsert=True,
        )

    @classmethod
    async def find_alias(cls, normalized_alias: str) -> Optional[dict]:
        return await db()[cls.coll].find_one({"type": "alias", "key": normalized_alias})

    @classmethod
    async def list(cls, ptype: Optional[str] = None) -> list[dict]:
        q = {"type": ptype} if ptype else {}
        cur = db()[cls.coll].find(q)
        return [d async for d in cur]
