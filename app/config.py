"""Central configuration.

Everything is read from environment variables (see .env.example).
No secrets are hard-coded. The object is instantiated once as ``settings``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv_int(name: str) -> list[int]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


@dataclass
class Settings:
    # ---- Telegram (userbot / MTProto client API) ----
    tg_api_id: int = field(default_factory=lambda: _int("TG_API_ID", 0))
    tg_api_hash: str = field(default_factory=lambda: os.getenv("TG_API_HASH", ""))
    tg_session: str = field(default_factory=lambda: os.getenv("TG_SESSION", ""))
    # The owning account's numeric user id. Commands are only accepted from this id.
    owner_id: int = field(default_factory=lambda: _int("OWNER_ID", 0))
    # Source chats/threads that are read as an event stream.
    source_chat_ids: list[int] = field(default_factory=lambda: _csv_int("SOURCE_CHAT_IDS"))
    # Where media cards are published.
    target_chat_id: int = field(default_factory=lambda: _int("TARGET_CHAT_ID", 0))
    target_topic_id: int = field(default_factory=lambda: _int("TARGET_TOPIC_ID", 0))
    command_prefix: str = field(default_factory=lambda: os.getenv("COMMAND_PREFIX", "."))

    # ---- MongoDB ----
    mongo_uri: str = field(default_factory=lambda: os.getenv("MONGO_URI", "mongodb://mongo:27017"))
    mongo_db: str = field(default_factory=lambda: os.getenv("MONGO_DB", "mediaindexer"))

    # ---- External metadata providers ----
    tmdb_api_key: str = field(default_factory=lambda: os.getenv("TMDB_API_KEY", ""))
    omdb_api_key: str = field(default_factory=lambda: os.getenv("OMDB_API_KEY", ""))
    mal_client_id: str = field(default_factory=lambda: os.getenv("MAL_CLIENT_ID", ""))
    tmdb_language: str = field(default_factory=lambda: os.getenv("TMDB_LANGUAGE", "de-DE"))
    provider_cache_ttl_days: int = field(default_factory=lambda: _int("PROVIDER_CACHE_TTL_DAYS", 30))

    # ---- Pipeline / processing rules ----
    max_lines: int = field(default_factory=lambda: _int("MAX_CONTENT_LINES", 3))
    queue_maxsize: int = field(default_factory=lambda: _int("QUEUE_MAXSIZE", 1000))
    item_timeout_seconds: int = field(default_factory=lambda: _int("ITEM_TIMEOUT_SECONDS", 45))
    recent_events_keep: int = field(default_factory=lambda: _int("RECENT_EVENTS_KEEP", 50))
    title_match_threshold: int = field(default_factory=lambda: _int("TITLE_MATCH_THRESHOLD", 86))
    provider_match_threshold: int = field(default_factory=lambda: _int("PROVIDER_MATCH_THRESHOLD", 78))
    classify_min_confidence: float = field(default_factory=lambda: _float("CLASSIFY_MIN_CONFIDENCE", 0.45))

    # ---- UI / posting ----
    tg_message_limit: int = field(default_factory=lambda: _int("TG_MESSAGE_LIMIT", 3900))
    # Telegram media captions are limited to 1024 UTF-16 units; keep a margin.
    tg_caption_limit: int = field(default_factory=lambda: _int("TG_CAPTION_LIMIT", 1000))
    episodes_full_limit: int = field(default_factory=lambda: _int("EPISODES_FULL_LIMIT", 20))
    episodes_block_limit: int = field(default_factory=lambda: _int("EPISODES_BLOCK_LIMIT", 100))
    episodes_group_limit: int = field(default_factory=lambda: _int("EPISODES_GROUP_LIMIT", 1000))

    # ---- Telegram outbound rate limiting (avoids FloodWaitError) ----
    # Minimum gap between any two outgoing writes (send/edit/delete). Bulk card
    # posting is paced to this so Telegram's flood limits are not tripped.
    tg_min_send_interval: float = field(default_factory=lambda: _float("TG_MIN_SEND_INTERVAL", 2.0))
    # Telethon waits out flood errors up to this many seconds transparently;
    # longer waits are handled (and logged) by the send gate instead.
    tg_flood_sleep_threshold: int = field(default_factory=lambda: _int("TG_FLOOD_SLEEP_THRESHOLD", 60))
    # How many times the gate will wait out a FloodWait on the same write before
    # giving up (the item stays dirty and is retried later by the healer).
    tg_flood_max_retries: int = field(default_factory=lambda: _int("TG_FLOOD_MAX_RETRIES", 5))

    # ---- Healing ----
    heal_interval_seconds: int = field(default_factory=lambda: _int("HEAL_INTERVAL_SECONDS", 900))
    pending_max_attempts: int = field(default_factory=lambda: _int("PENDING_MAX_ATTEMPTS", 5))

    # ---- Logging ----
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def validate(self) -> list[str]:
        """Return a list of fatal configuration problems (empty == OK)."""
        problems: list[str] = []
        if not self.tg_api_id:
            problems.append("TG_API_ID is missing")
        if not self.tg_api_hash:
            problems.append("TG_API_HASH is missing")
        if not self.tg_session:
            problems.append("TG_SESSION is missing (run scripts/generate_session.py)")
        if not self.owner_id:
            problems.append("OWNER_ID is missing")
        if not self.source_chat_ids:
            problems.append("SOURCE_CHAT_IDS is empty")
        if not self.target_chat_id:
            problems.append("TARGET_CHAT_ID is missing")
        return problems


settings = Settings()
