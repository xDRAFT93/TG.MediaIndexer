"""Deterministic Media Card templates (Telegram HTML parse mode).

The layout is fixed and mirrors the reference card:

    <b>Title (Year)</b>

    <blockquote>Overview...</blockquote>

    🎬 Genres: a, b, c
    ⭐ Bewertung: 7.5 (3144 votes)
    📅 Erstveröffentlichung: 2005-03-26
    ⏳ Laufzeit: 45 Minuten

Free-form formatting is forbidden. Every line is only emitted when its data
exists — in particular the runtime line is omitted when runtime is unknown
(this fixes the "Laufzeit: None Minuten" artefact in the reference screenshot).
"""
from __future__ import annotations

from typing import Optional

from ..storage.models import Media

EMOJI_GENRES = "\U0001F3AC"   # 🎬
EMOJI_RATING = "\u2B50"        # ⭐
EMOJI_RELEASE = "\U0001F4C5"  # 📅
EMOJI_RUNTIME = "\u23F3"       # ⏳
EMOJI_EPISODES = "\U0001F39E\uFE0F"  # 🎞️
EMOJI_RELEASES = "\U0001F4E6"  # 📦
EMOJI_SOURCE = "\U0001F517"    # 🔗
EMOJI_CONT = "\u27A1\uFE0F"    # ➡️


def esc(text: Optional[str]) -> str:
    """Escape text for Telegram HTML parse mode."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def title_line(media: Media) -> str:
    if media.year:
        return f"<b>{esc(media.title)} ({media.year})</b>"
    return f"<b>{esc(media.title)}</b>"


def overflow_header(media: Media, part: int) -> str:
    """Header for overflow posts — the title must never be missing."""
    return f"{title_line(media)} {EMOJI_CONT} ({part})"


def overview_block(media: Media) -> str:
    if not media.overview:
        return ""
    return f"<blockquote>{esc(media.overview)}</blockquote>"


def metadata_block(media: Media) -> str:
    lines: list[str] = []
    if media.genres:
        lines.append(f"{EMOJI_GENRES} Genres: {esc(', '.join(media.genres))}")
    if media.rating is not None:
        rating = _fmt_number(media.rating)
        if media.votes:
            lines.append(f"{EMOJI_RATING} Bewertung: {rating} ({media.votes} votes)")
        else:
            lines.append(f"{EMOJI_RATING} Bewertung: {rating}")
    if media.release_date:
        lines.append(f"{EMOJI_RELEASE} Erstver\u00f6ffentlichung: {esc(media.release_date)}")
    if media.runtime is not None:  # omit when unknown (fixes "None Minuten")
        lines.append(f"{EMOJI_RUNTIME} Laufzeit: {media.runtime} Minuten")
    return "\n".join(lines)


def _fmt_number(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.1f}"
