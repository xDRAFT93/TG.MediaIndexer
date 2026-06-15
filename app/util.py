"""Small shared helpers used across modules."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Deterministic ASCII slug used for canonical keys."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().strip()
    value = _SLUG_RE.sub("-", value)
    return value.strip("-")


def normalize_title(value: str) -> str:
    """Loose normalisation used for fuzzy comparisons (not for storage keys)."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def first_n_lines(text: str | None, n: int) -> str:
    """Return only the first ``n`` non-empty-trimmed lines of ``text``.

    Implements the hard rule: never analyse more than N lines of message body.
    """
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[:n])


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
