"""MongoDB connection layer.

Uses ``pymongo.AsyncMongoClient`` (the PyMongo Async API). Motor reached
end-of-life on 2026-05-14, so it is intentionally NOT used.
"""
from __future__ import annotations

from typing import Optional

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("storage.database")

_client: Optional[AsyncMongoClient] = None
_db: Optional[AsyncDatabase] = None


async def connect() -> AsyncDatabase:
    """Connect (idempotent) and ensure indexes exist."""
    global _client, _db
    if _db is not None:
        return _db
    log.info("Connecting to MongoDB at %s / db=%s", settings.mongo_uri, settings.mongo_db)
    _client = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
    _db = _client[settings.mongo_db]
    await _ensure_indexes(_db)
    log.info("MongoDB ready")
    return _db


def db() -> AsyncDatabase:
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


async def close() -> None:
    global _client, _db
    if _client is not None:
        await _client.close()
    _client = None
    _db = None


async def _ensure_indexes(database: AsyncDatabase) -> None:
    await database.media.create_index([("canonical_key", ASCENDING)], unique=True)
    await database.media.create_index([("ui_dirty", ASCENDING)])
    await database.media.create_index([("media_type", ASCENDING)])

    await database.episodes.create_index([("canonical_key", ASCENDING)], unique=True)
    await database.episodes.create_index([("media_id", ASCENDING), ("season", ASCENDING), ("episode", ASCENDING)])

    # An event is unique per (chat, message). Guarantees idempotent ingest.
    await database.events.create_index([("chat_id", ASCENDING), ("message_id", ASCENDING)], unique=True)
    await database.events.create_index([("stage", ASCENDING)])
    await database.events.create_index([("created_at", DESCENDING)])

    await database.pending_events.create_index([("chat_id", ASCENDING), ("message_id", ASCENDING)], unique=True)
    await database.pending_events.create_index([("created_at", ASCENDING)])

    await database.thread_state.create_index([("chat_id", ASCENDING), ("thread_id", ASCENDING)])

    await database.posts.create_index([("media_id", ASCENDING), ("part_index", ASCENDING)])
    await database.posts.create_index([("chat_id", ASCENDING), ("message_id", ASCENDING)])

    await database.provider_cache.create_index([("key", ASCENDING)], unique=True)
    await database.provider_cache.create_index([("created_at", ASCENDING)])

    await database.patterns.create_index([("type", ASCENDING), ("key", ASCENDING)])
