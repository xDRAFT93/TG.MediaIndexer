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
from . import templates as T


def build_card(media: Media, episodes: list[Episode]) -> str:
    parts: list[str] = [T.title_line(media)]

    overview = T.overview_block(media)
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
    elif total <= settings.episodes_block_limit:
        body = _episodes_blocks(by_season, seasons)
    elif total <= settings.episodes_group_limit:
        body = _episodes_grouped(by_season, seasons)
    else:
        body = ""  # overview only

    return f"{header}\n{body}".rstrip() if body else header


def _episodes_full(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        for ep in sorted(by_season[s], key=lambda e: e.episode):
            code = f"S{ep.season:02d}E{ep.episode:02d}"
            if ep.title:
                lines.append(f"{code} \u2014 {T.esc(ep.title)}")
            else:
                lines.append(code)
    return "\n".join(lines)


def _episodes_blocks(by_season, seasons) -> str:
    lines: list[str] = []
    for s in seasons:
        eps = sorted(e.episode for e in by_season[s])
        ranges = _compress_ranges(eps)
        lines.append(f"Staffel {s:02d} ({len(eps)}): {ranges}")
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
        lines.append(f"\u2022 {T.esc(_release_label(rel))}")
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
        bits.append(f"Daten: {media.provider_used}")
    return " \u00b7 ".join(bits)
