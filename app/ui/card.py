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
        section = _episode_section(episodes, media.metadata_resolved)
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
def _episode_section(episodes: list[Episode], resolved: bool = True) -> str:
    if not episodes:
        return ""
    total = len(episodes)
    by_season: dict[int, list[Episode]] = defaultdict(list)
    for ep in episodes:
        by_season[ep.season].append(ep)
    seasons = sorted(by_season)

    body = "\n".join(_episode_blocks(by_season, seasons))
    if not resolved:
        # Unresolved entries omit the "Episoden: N in M Staffel(n)" summary line.
        return body
    header = f"{T.EMOJI_EPISODES} Episoden: {total} in {len(seasons)} Staffel(n)"
    return f"{header}\n{body}"


# Visible-text budget for a single collapsible block. Telegram counts only the
# visible text against its limit, so a block of episode links is small even when
# the underlying HTML (deep-link URLs) is large. Leave room for the overflow
# header the post manager may prepend.
def _block_budget() -> int:
    return max(400, settings.tg_message_limit - 150)


_QUALITY_RANK = {
    "2160p": 5, "4k": 5, "uhd": 5, "1080p": 4, "1080i": 4,
    "720p": 3, "576p": 2, "480p": 1, "360p": 0,
}


def _release_link(rel: Release) -> str:
    return L.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)


def _episode_token(ep: Episode) -> str:
    """Linked episode number. When the same episode exists in several versions
    (e.g. a 720p file later replaced by a 1080p one), the number links to the
    best version and each additional version is appended as its own linked tag
    — so every source post stays reachable, separated by quality."""
    code = f"E{ep.episode:02d}"
    rels = list(ep.releases)
    if not rels:
        return T.link("", code)
    rels.sort(key=lambda r: (_QUALITY_RANK.get((r.quality or "").lower(), 0),
                             r.message_id or 0), reverse=True)
    token = T.link(_release_link(rels[0]), code)
    for i, r in enumerate(rels[1:], start=2):
        label = r.quality or f"v{i}"
        # A leading space keeps the version tag separately tappable from E01.
        token += " " + T.link(_release_link(r), f"[{label}]")
    return token


def _episode_blocks(by_season, seasons) -> list[str]:
    """One or more collapsed blockquotes per season. The season header sits
    INSIDE the block on its own line; the episodes follow as space-separated,
    individually-linked numbers (``E01 E02 E03 …``) that wrap naturally — so no
    line is wasted per episode. A season is split across blocks only when its
    VISIBLE length would exceed one Telegram message."""
    budget = _block_budget()
    blocks: list[str] = []
    for s in seasons:
        eps = sorted(by_season[s], key=lambda e: e.episode)
        tokens = [(ep, _episode_token(ep)) for ep in eps]
        for chunk, multi in _pack_by_visible(tokens, budget, header_reserve=40):
            lo, hi = chunk[0][0].episode, chunk[-1][0].episode
            if multi:
                head = f"<b>Staffel {s:02d}</b> (E{lo:02d}\u2013E{hi:02d})"
            else:
                head = f"<b>Staffel {s:02d}</b> ({len(chunk)} Ep.)"
            inner = head + "\n" + " ".join(html for _ep, html in chunk)
            blocks.append(T.expandable_quote(inner))
    return blocks


def _pack_by_visible(items, budget: int, header_reserve: int,
                     base_entities: int = 2):
    """Greedily group (key, html) items so each group fits within BOTH limits:
    the visible-text budget (plus a header reserve) AND Telegram's per-message
    entity cap. ``base_entities`` accounts for the wrapper the group will get
    (a blockquote + a bold header = 2). Yields (group, is_multi)."""
    max_entities = max(8, settings.tg_max_entities - 2)  # leave room for overflow header
    groups: list[list] = []
    cur: list = []
    cur_vis = header_reserve
    cur_ent = base_entities
    for key, html in items:
        add_vis = T.visible_len(html) + 1  # +1 for the separator
        add_ent = T.entity_count(html)
        if cur and (cur_vis + add_vis > budget or cur_ent + add_ent > max_entities):
            groups.append(cur)
            cur, cur_vis, cur_ent = [], header_reserve, base_entities
        cur.append((key, html))
        cur_vis += add_vis
        cur_ent += add_ent
    if cur:
        groups.append(cur)
    multi = len(groups) > 1
    for g in groups:
        yield g, multi


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
    header_text = f"{T.EMOJI_RELEASES} Releases: {len(releases)}"
    lines: list[tuple[int, str]] = []
    for i, rel in enumerate(releases):
        href = L.tg_message_link(rel.chat_id, rel.message_id, rel.thread_id)
        lines.append((i, f"\u2022 {T.link(href, _release_label(rel))}"))
    out: list[str] = []
    for idx, (chunk, _multi) in enumerate(_pack_by_visible(lines, _block_budget(),
                                                           header_reserve=40)):
        body = "\n".join(html for _i, html in chunk)
        # The "Releases: N" header lives INSIDE the (first) collapsed block,
        # together with the linked film versions.
        inner = f"{header_text}\n{body}" if idx == 0 else body
        out.append(T.expandable_quote(inner))
    return "\n".join(out)


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
    if media.sources:
        if len(media.sources) == 1:
            # A single source reads "Quelle" with the word itself linked.
            href = _source_href(media.sources[0])
            bits.append(f"{T.EMOJI_SOURCE} {T.link(href, 'Quelle')}")
        else:
            bits.append(f"{T.EMOJI_SOURCE} Quellen: {_sources_line(media.sources)}")
    if media.provider_used:
        external_id = (media.providers or {}).get(media.provider_used, "")
        href = L.provider_link(media.provider_used, external_id, media.media_type)
        bits.append(f"Daten: {T.link(href, L.provider_label(media.provider_used))}")
    return " \u00b7 ".join(bits)


def _source_href(src) -> str:
    d = src if isinstance(src, dict) else (src.to_dict() if hasattr(src, "to_dict") else {})
    return L.tg_message_link(
        d.get("chat_id"),
        d.get("first_message_id") or d.get("last_message_id"),
        d.get("thread_id"),
    )


def _sources_line(sources: list) -> str:
    """Render each source as a clickable index (1 2 3 …) linking back to its
    source thread/post; falls back to a plain number when not deep-linkable."""
    parts: list[str] = []
    for i, src in enumerate(sources, 1):
        parts.append(T.link(_source_href(src), str(i)))
    return " ".join(parts)
