"""Card assembly.

Builds the deterministic card for a media from its metadata, episodes and
releases. Episode rendering scales with volume per the spec:

    <= 20    full list        (S01E01 - Title)
    <= 100   per-season blocks (ranges, counts)
    <= 1000  grouped          (one line per season with a count)
    > 1000   overview only    (totals)

Every clickable reference is an inline ``<a href=...>`` link rendered *inside*
the surrounding text — the source post of each episode / film release, the
"sources" count in the footer, and the metadata provider (TMDb / IMDb / MAL …).
Bare, separate URLs are never emitted.

The card is produced as a list of :class:`CardBlock`s. The post manager packs
those blocks (line by line) across the photo root post and any linked overflow
posts. ``build_card`` returns the same content as a single string (used for
previews / tests).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..config import settings
from ..storage.models import Episode, Media, MediaType, Release
from . import templates as T


@dataclass
class CardBlock:
    """A logical section of the card.

    ``splittable`` marks blocks whose lines may be distributed across several
    posts (episode lists, metadata). Atomic blocks (title, the single-line
    collapsed overview, footer) are each a single physical line and therefore
    never split mid-tag regardless of the flag.
    """

    text: str
    splittable: bool = False


def build_blocks(media: Media, episodes: list[Episode]) -> list[CardBlock]:
    blocks: list[CardBlock] = [CardBlock(T.title_line(media))]

    overview = T.overview_block(media)
    if overview:
        blocks.append(CardBlock(overview))

    meta = T.metadata_block(media)
    if meta:
        blocks.append(CardBlock(meta, splittable=True))

    if media.media_type in (MediaType.SERIES, MediaType.ANIME):
        section = _episode_section(episodes)
    else:
        section = _release_section(media.releases)
    if section:
        blocks.append(CardBlock(section, splittable=True))

    footer = _footer(media)
    if footer:
        blocks.append(CardBlock(footer))

    return blocks


def build_card(media: Media, episodes: list[Episode]) -> str:
    """Flatten the card to a single string (preview / compatibility helper)."""
    return "\n\n".join(b.text for b in build_blocks(media, episodes)).strip()


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
    elif total <= settings.episodes_block_limit:
        body = _episodes_blocks(by_season, seasons)
    elif total <= settings.episodes_group_limit:
        body = _episodes_grouped(by_season, seasons)
    else:
        body = ""  # overview only

    return f"{header}\n{body}".rstrip() if body else header


def _episode_source_url(ep: Episode) -> str:
    """Deep link to the first source post that carries this episode."""
    for rel in ep.releases:
        url = T.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)
        if url:
            return url
    return ""


def _episodes_full(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        for ep in sorted(by_season[s], key=lambda e: e.episode):
            code = f"S{ep.season:02d}E{ep.episode:02d}"
            code_html = T.link(_episode_source_url(ep), code)  # clickable -> source
            if ep.title:
                lines.append(f"{code_html} \u2014 {T.esc(T.clamp(ep.title, 200))}")
            else:
                lines.append(code_html)
    return "\n".join(lines)


def _episodes_blocks(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        eps = sorted(e.episode for e in by_season[s])
        ranges = _compress_ranges(eps)
        lines.append(f"Staffel {s:02d} ({len(eps)}): {T.esc(ranges)}")
    return "\n".join(lines)


def _episodes_grouped(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        eps = by_season[s]
        lo = min(e.episode for e in eps)
        hi = max(e.episode for e in eps)
        lines.append(f"Staffel {s:02d}: {len(eps)} Episoden (E{lo:02d}-E{hi:02d})")
    return "\n".join(lines)


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
        url = T.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)
        lines.append(f"\u2022 {T.link(url, T.clamp(_release_label(rel), 200))}")
    return "\n".join(lines)


def _release_label(rel: Release) -> str:
    bits = [b for b in (rel.quality, rel.source_tag, rel.codec, rel.group) if b]
    if bits:
        return " / ".join(bits)
    return rel.file_name or "release"


# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #
def _first_source_url(media: Media) -> str:
    for src in media.sources:
        if not isinstance(src, dict):
            continue
        url = T.tg_message_link(
            src.get("chat_id"), src.get("first_message_id"), src.get("thread_id")
        )
        if url:
            return url
    return ""


def _footer(media: Media) -> str:
    bits: list[str] = []
    n_sources = len(media.sources)
    if n_sources:
        bits.append(f"{T.EMOJI_SOURCE} Quellen: {T.link(_first_source_url(media), str(n_sources))}")
    prov_url, prov_label = T.provider_ref(media)
    if prov_label:
        bits.append(f"Daten: {T.link(prov_url, prov_label)}")
    return " \u00b7 ".join(bits)
