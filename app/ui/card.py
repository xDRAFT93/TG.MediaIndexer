"""Card assembly.

Builds the full deterministic card text for a media from its metadata, episodes
and releases. Episode rendering scales with volume per the spec:

    <= 20    full list        (S01E01 - Title)
    <= 100   per-season blocks (ranges, counts)
    <= 1000  grouped          (one line per season with a count)
    > 1000   overview only    (totals)

The post manager later splits this text across the root post and, if needed,
linked overflow posts.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..config import settings
from ..storage.models import Episode, Media, MediaType, Release
from . import links as L
from . import templates as T


def build_card(media: Media, episodes: list[Episode]) -> str:
    parts: list[str] = [T.title_line(media)]

    overview = T.overview_block(media, settings.overview_max_chars)
    if overview:
        parts.append("")
        parts.append(overview)

    meta = T.metadata_block(media)
    if meta:
        parts.append("")
        parts.append(meta)

    if media.media_type in (MediaType.SERIES, MediaType.ANIME):
        section = _episode_section(episodes)
        if section:
            parts.append("")
            parts.append(section)
    else:
        section = _release_section(media.releases)
        if section:
            parts.append("")
            parts.append(section)

    footer = _footer(media)
    if footer:
        parts.append("")
        parts.append(footer)

    return "\n".join(parts).strip()


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #
def _episode_section(episodes: list[Episode]) -> str:
    if not episodes:
        return ""
    total = len(episodes)
    by_season: dict[int, list[Episode]] = defaultdict(list)
    for ep in episodes:
        by_season[ep.season].append(ep)
    seasons = sorted(by_season)

    header = f"{T.EMOJI_EPISODES} Episoden: {total} in {len(seasons)} Staffel(n)"

    if total <= settings.episodes_full_limit:
        body = _episodes_full(by_season, seasons)
    elif total <= settings.episodes_link_limit:
        # Every episode stays individually clickable, packed into a per-season
        # collapsible blockquote so the post stays compact but loses no links.
        body = _episodes_seasons_linked(by_season, seasons)
    else:
        # Very large series: one line per season, first episode linked as anchor.
        body = _episodes_overview_linked(by_season, seasons)

    return f"{header}\n{body}".rstrip() if body else header


def _episodes_full(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        for ep in sorted(by_season[s], key=lambda e: e.episode):
            code = f"S{ep.season:02d}E{ep.episode:02d}"
            label = f"{code} \u2014 {ep.title}" if ep.title else code
            lines.append(T.link(_episode_source(ep), label))
    return "\n".join(lines)


def _episode_source(ep: Episode) -> str:
    """Deep link to the source post of an episode's first known release."""
    for rel in ep.releases:
        href = L.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)
        if href:
            return href
    return ""


def _episodes_seasons_linked(by_season, seasons) -> str:
    """One or more collapsible blockquotes per season, every episode linked.

    A season whose linked list would exceed one Telegram message is split into
    several blockquotes (E01–E42, E43–E84, …) rather than dropping links — the
    body of each blockquote is a single physical line so the post splitter can
    never tear a ``<blockquote>`` pair across two posts.
    """
    # Leave room for the open/close tags, a season header line and the overflow
    # header the post manager may prepend, all within one Telegram message.
    max_inner = max(400, settings.tg_message_limit - 380)
    lines: list[str] = []
    for s in seasons:
        eps = sorted(by_season[s], key=lambda e: e.episode)
        chunks = _pack_tokens(eps, max_inner)
        multi = len(chunks) > 1
        for chunk in chunks:
            lo, hi = chunk[0][0].episode, chunk[-1][0].episode
            if multi:
                head = f"<b>Staffel {s:02d}</b> (E{lo:02d}\u2013E{hi:02d}, {len(chunk)})"
            else:
                head = f"<b>Staffel {s:02d}</b> ({len(chunk)} Ep.)"
            inner = " ".join(tok for _ep, tok in chunk)
            lines.append(f"{head}\n<blockquote expandable>{inner}</blockquote>")
    return "\n".join(lines)


def _pack_tokens(eps: list[Episode], max_inner: int):
    """Greedily group episodes so each group's joined links fit ``max_inner``."""
    groups: list[list] = []
    cur: list = []
    cur_len = 0
    for e in eps:
        tok = T.link(_episode_source(e), f"E{e.episode:02d}")
        add = len(tok) + (1 if cur else 0)
        if cur and cur_len + add > max_inner:
            groups.append(cur)
            cur, cur_len = [], 0
            add = len(tok)
        cur.append((e, tok))
        cur_len += add
    if cur:
        groups.append(cur)
    return groups


def _episodes_overview_linked(by_season, seasons) -> str:
    """Compact overview: one line per season, first episode linked as anchor."""
    if len(seasons) > 200:
        return ""  # absurd season count -> header-only overview
    return "\n".join(
        _season_fallback_line(s, sorted(by_season[s], key=lambda e: e.episode))
        for s in seasons
    )


def _season_fallback_line(s: int, eps: list[Episode]) -> str:
    rng = _compress_ranges([e.episode for e in eps])
    marker = T.link(_episode_source(eps[0]), f"S{s:02d}E{eps[0].episode:02d}")
    return f"<b>Staffel {s:02d}</b> ({len(eps)} Ep.): {rng} \u00b7 ab {marker}"


def _compress_ranges(nums: list[int]) -> str:
    if not nums:
        return ""
    nums = sorted(set(nums))
    out: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"E{start:02d}" if start == prev else f"E{start:02d}-E{prev:02d}")
        start = prev = n
    out.append(f"E{start:02d}" if start == prev else f"E{start:02d}-E{prev:02d}")
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# Film releases
# --------------------------------------------------------------------------- #
def _release_section(releases: list[Release]) -> str:
    if not releases:
        return ""
    header = f"{T.EMOJI_RELEASES} Releases: {len(releases)}"
    if len(releases) > settings.episodes_full_limit:
        return header
    lines = [header]
    for rel in releases:
        href = L.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)
        lines.append(f"\u2022 {T.link(href, _release_label(rel))}")
    return "\n".join(lines)


def _release_label(rel: Release) -> str:
    bits = [b for b in (rel.quality, rel.source_tag, rel.codec, rel.group) if b]
    if bits:
        return " / ".join(bits)
    return rel.file_name or "release"


# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #
def _footer(media: Media) -> str:
    bits: list[str] = []
    n_sources = len(media.sources)
    if n_sources:
        bits.append(f"{T.EMOJI_SOURCE} Quellen: {n_sources}")
    if media.provider_used:
        external_id = (media.providers or {}).get(media.provider_used, "")
        href = L.provider_link(media.provider_used, external_id, media.media_type)
        bits.append(f"Daten: {T.link(href, L.provider_label(media.provider_used))}")
    return " \u00b7 ".join(bits)
