"""Deterministic Media Card templates (Telegram HTML parse mode).

The layout is fixed and mirrors the reference card:

    <b>Title (Year)</b>

    <blockquote>Overview...</blockquote>      (rendered collapsed/expandable)

    🎬 Genres: a, b, c
    ⭐ Bewertung: 7.5 (3144 votes)
    📅 Erstveröffentlichung: 2005-03-26
    ⏳ Laufzeit: 45 Minuten

Free-form formatting is forbidden. Every line is only emitted when its data
exists — in particular the runtime line is omitted when runtime is unknown
(this fixes the "Laufzeit: None Minuten" artefact in the reference screenshot).

All clickable references (source posts, the metadata provider) are produced as
inline ``<a href=...>`` links *inside* the surrounding text — never as separate
bare URLs.
"""
from __future__ import annotations

import html as _htmllib
import re
from typing import Optional

from ..storage.models import Media, MediaType

EMOJI_GENRES = "\U0001F3AC"   # 🎬
EMOJI_RATING = "\u2B50"        # ⭐
EMOJI_RELEASE = "\U0001F4C5"  # 📅
EMOJI_RUNTIME = "\u23F3"       # ⏳
EMOJI_EPISODES = "\U0001F39E\uFE0F"  # 🎞️
EMOJI_RELEASES = "\U0001F4E6"  # 📦
EMOJI_SOURCE = "\U0001F517"    # 🔗
EMOJI_CONT = "\u27A1\uFE0F"    # ➡️

# Upper bound on the overview length (visible chars). The quote is collapsed, so
# the full text stays available on tap; this only keeps a single physical line.
OVERVIEW_MAX = 600

# Friendly, link-target-appropriate labels for the metadata providers.
_PROVIDER_LABEL = {
    "tmdb": "TMDb",
    "omdb": "IMDb",
    "jikan": "MyAnimeList",
    "anilist": "AniList",
    "kitsu": "Kitsu",
}

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# Escaping / length / links
# --------------------------------------------------------------------------- #
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


def visible_text(html: str) -> str:
    """The user-visible text of an HTML snippet (tags dropped, entities decoded)."""
    return _htmllib.unescape(_TAG_RE.sub("", html or ""))


def visible_len(html: str) -> int:
    """Telegram-counted length: UTF-16 code units of the *visible* text.

    Telegram measures message/caption length in UTF-16 units and does not count
    HTML tags or ``href`` values, so this is what limits must be checked against.
    """
    return len(visible_text(html).encode("utf-16-le")) // 2


def clamp(text: Optional[str], n: int) -> str:
    """Truncate raw (unescaped) text to at most ``n`` characters with an ellipsis."""
    if not text:
        return ""
    t = str(text)
    if len(t) <= n:
        return t
    return t[: max(0, n - 1)].rstrip() + "\u2026"


def link(url: str, text: str) -> str:
    """Inline link. ``text`` is raw and gets escaped here; returns plain escaped
    text when no URL is available (so callers never double-escape)."""
    safe = esc(text)
    if not url:
        return safe
    return f'<a href="{esc(url)}">{safe}</a>'


def tg_message_link(chat_id, message_id, thread_id=None) -> str:
    """Build a ``https://t.me/c/<internal>/<topic?>/<message>`` deep link.

    Works for the private super-groups / forum topics this bot reads. ``chat_id``
    in ``-100…`` form is converted to the internal id Telegram uses in links.
    """
    if not chat_id or not message_id:
        return ""
    cid = str(chat_id)
    if cid.startswith("-100"):
        internal = cid[4:]
    elif cid.startswith("-"):
        internal = cid[1:]
    else:
        internal = cid
    if not internal.isdigit():
        return ""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return ""
    if thread_id:
        try:
            tid = int(thread_id)
        except (TypeError, ValueError):
            tid = 0
        if tid:
            return f"https://t.me/c/{internal}/{tid}/{mid}"
    return f"https://t.me/c/{internal}/{mid}"


def _provider_url(provider: str, external_id: str, media_type: MediaType) -> str:
    if not external_id:
        return ""
    p = (provider or "").lower()
    eid = str(external_id)
    if p == "tmdb":
        kind = "movie" if media_type == MediaType.FILM else "tv"
        return f"https://www.themoviedb.org/{kind}/{eid}"
    if p == "omdb":  # OMDb is IMDb-backed; external_id is the imdbID (tt…)
        return f"https://www.imdb.com/title/{eid}/"
    if p == "jikan":  # MyAnimeList id
        return f"https://myanimelist.net/anime/{eid}"
    if p == "anilist":
        return f"https://anilist.co/anime/{eid}"
    if p == "kitsu":
        return f"https://kitsu.io/anime/{eid}"
    return ""


def provider_ref(media: Media) -> tuple[str, str]:
    """Return ``(url, label)`` for the metadata source, e.g. ``(tmdb-url, "TMDb")``."""
    prov = (media.provider_used or "").lower()
    eid = ""
    if prov:
        eid = media.providers.get(media.provider_used) or media.providers.get(prov) or ""
    if not eid and media.providers:
        # Fall back to whatever single provider/id pair we stored.
        first_prov, first_eid = next(iter(media.providers.items()))
        prov, eid = (first_prov or "").lower(), first_eid
    if not prov:
        return "", ""
    label = _PROVIDER_LABEL.get(prov, media.provider_used or prov)
    return _provider_url(prov, eid, media.media_type), label


# --------------------------------------------------------------------------- #
# Card lines
# --------------------------------------------------------------------------- #
def title_line(media: Media) -> str:
    title = clamp(media.title, 220)
    if media.year:
        return f"<b>{esc(title)} ({media.year})</b>"
    return f"<b>{esc(title)}</b>"


def overflow_header(media: Media, part: int) -> str:
    """Header for overflow posts — the title must never be missing."""
    return f"{title_line(media)} {EMOJI_CONT} ({part})"


def overview_block(media: Media) -> str:
    """The overview as a single-line, collapsed/expandable blockquote.

    Internal whitespace is collapsed to single spaces so the block is one
    physical line (keeps the splitter simple and tag-safe). The ``collapsed``
    rendering is applied by the custom parse mode in ``telegram.formatting``.
    """
    if not media.overview:
        return ""
    text = " ".join(str(media.overview).split())
    text = clamp(text, OVERVIEW_MAX)
    return f"<blockquote>{esc(text)}</blockquote>"


def metadata_block(media: Media) -> str:
    lines: list[str] = []
    if media.genres:
        lines.append(f"{EMOJI_GENRES} Genres: {esc(clamp(', '.join(media.genres), 300))}")
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
