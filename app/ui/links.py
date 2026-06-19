"""Builders for clickable links embedded in the card (Telegram HTML).

Two kinds of links are produced:

  * ``tg_message_link`` — a deep link to the *source* Telegram post a file came
    from, so each release / episode in the card is clickable back to its origin.
  * ``provider_link`` — a link to the metadata provider's page (TMDb, IMDb via
    OMDb, MyAnimeList via Jikan, AniList, Kitsu).

Links are returned as plain URLs; the templates wrap them in ``<a>`` tags.
"""
from __future__ import annotations

from typing import Optional

from ..storage.models import MediaType


def tg_message_link(
    chat_id: Optional[int],
    message_id: Optional[int],
    topic_id: Optional[int] = None,
) -> str:
    """Build a ``https://t.me/c/<internal>/[topic/]<msg>`` deep link.

    Supergroup / channel ids carry a ``-100`` prefix in the Bot/MTProto id
    space; the public deep-link form uses the id with that prefix stripped.
    Basic groups cannot be deep-linked and yield an empty string.
    """
    if not chat_id or not message_id:
        return ""
    s = str(chat_id)
    if s.startswith("-100"):
        internal = s[4:]
    elif s.startswith("-"):
        # basic group — not linkable
        return ""
    else:
        internal = s
    if not internal:
        return ""
    if topic_id:
        return f"https://t.me/c/{internal}/{topic_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


_PROVIDER_LABEL = {
    "tmdb": "TMDb",
    "omdb": "IMDb",
    "jikan": "MyAnimeList",
    "anilist": "AniList",
    "kitsu": "Kitsu",
    "audnexus": "Audible",
    "googlebooks": "Google Books",
    "dnb": "DNB",
    "openlibrary": "Open Library",
}


def provider_label(provider: str) -> str:
    return _PROVIDER_LABEL.get((provider or "").lower(), provider or "")


def provider_link(provider: str, external_id: str, media_type: MediaType) -> str:
    """Canonical URL for the provider page that backs this media."""
    p = (provider or "").lower()
    eid = str(external_id or "").strip()
    if not eid:
        return ""
    if p == "tmdb":
        kind = "movie" if media_type == MediaType.FILM else "tv"
        return f"https://www.themoviedb.org/{kind}/{eid}"
    if p == "omdb":  # external id is the IMDb id (tt...)
        return f"https://www.imdb.com/title/{eid}/"
    if p == "jikan":  # external id is the MAL id
        return f"https://myanimelist.net/anime/{eid}"
    if p == "anilist":
        return f"https://anilist.co/anime/{eid}"
    if p == "kitsu":
        return f"https://kitsu.io/anime/{eid}"
    if p == "audnexus":  # external id is the Audible ASIN
        return f"https://www.audible.de/pd/{eid}"
    if p == "googlebooks":
        return f"https://books.google.com/books?id={eid}"
    if p == "dnb":
        return f"https://d-nb.info/{eid}"
    if p == "openlibrary":  # external id like "/works/OL123W"
        return f"https://openlibrary.org{eid}" if eid.startswith("/") else f"https://openlibrary.org/{eid}"
    return ""
