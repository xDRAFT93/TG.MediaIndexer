"""Title extraction.

Priority parsers:
  * anitopy  -> anime file names (fansub conventions)
  * guessit  -> film/series scene file names
Both are optional; a deterministic regex fallback is always available so the
system runs without them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .episodes import EpisodeInfo, episode_marker_start, parse_episode
from .patterns import (
    ANIME_GROUP_RE,
    ANIME_HINT_RE,
    BRACKET_RE,
    EP_SCRUB_RES,
    HASHTAG_RE,
    JUNK_LEADING_GLUE,
    JUNK_STRONG_EXACT,
    JUNK_STRONG_PREFIXES,
    JUNK_WEAK_TOKENS,
    MULTISPACE_RE,
    RELEASE_TOKENS,
    SEPARATOR_RE,
    TRAILING_GROUP_RE,
    VIDEO_EXTENSIONS,
    YEAR_RE,
)

try:
    import anitopy  # type: ignore
    _HAS_ANITOPY = True
except Exception:  # pragma: no cover
    _HAS_ANITOPY = False

try:
    from guessit import guessit  # type: ignore
    _HAS_GUESSIT = True
except Exception:  # pragma: no cover
    _HAS_GUESSIT = False


@dataclass
class Extraction:
    title: str = ""
    year: Optional[int] = None
    tags: list[str] = field(default_factory=list)
    episode: EpisodeInfo = field(default_factory=EpisodeInfo)
    anime_signal: bool = False
    group: str = ""
    source_field: str = ""   # file_name | caption | message_text
    has_own_marker: bool = False  # this source contained a season/episode marker
    raw: str = ""

    @property
    def has_title(self) -> bool:
        return bool(self.title.strip())


def _norm_token(tok: str) -> str:
    return "".join(ch for ch in tok.lower().replace("ı", "i") if ch.isalnum())


def strip_junk_prefix(title: str) -> str:
    """Remove leading downloader/site noise (Y2Mate, vıvo Watch, ...) from a
    title without eating real leading words like "Watch Dogs" or "The Ting"."""
    if not title:
        return title
    tokens = title.split()
    out = list(tokens)
    dropped_strong = False
    while out:
        n = _norm_token(out[0])
        if not n:
            out.pop(0)
            continue
        if n in JUNK_STRONG_EXACT or any(n.startswith(p) for p in JUNK_STRONG_PREFIXES):
            out.pop(0)
            dropped_strong = True
            continue
        if dropped_strong and (n in JUNK_WEAK_TOKENS or n in JUNK_LEADING_GLUE):
            out.pop(0)
            continue
        break
    result = " ".join(out).strip(" -–_")
    return result or title  # never strip everything away


def _strip_extension(name: str) -> str:
    if "." in name:
        ext = name.rsplit(".", 1)[1].lower()
        if ext in VIDEO_EXTENSIONS or (ext.isalnum() and len(ext) <= 4):
            return name.rsplit(".", 1)[0]
    return name


def _extract_hashtags(text: str) -> list[str]:
    return [m.group(1).lower() for m in HASHTAG_RE.finditer(text or "")]


def _fallback_title(raw: str) -> tuple[str, Optional[int]]:
    """Deterministic title + year extraction without external parsers."""
    year_match = YEAR_RE.search(raw)
    year = int(year_match.group(1)) if year_match else None

    s = BRACKET_RE.sub(" ", raw)
    for rx in EP_SCRUB_RES:
        s = rx.sub(" ", s)
    s = SEPARATOR_RE.sub(" ", s)
    tokens = s.split()

    title_tokens: list[str] = []
    for tok in tokens:
        low = tok.lower().strip("-–_")
        if YEAR_RE.fullmatch(tok):
            break
        if low in RELEASE_TOKENS:
            break
        # stop at a bare release group like "GROUP" only if we already have words
        if title_tokens and TRAILING_GROUP_RE.search("-" + tok):
            # heuristic: ALL CAPS short trailing token after enough title
            if tok.isupper() and len(tok) <= 6 and len(title_tokens) >= 2:
                break
        title_tokens.append(tok)

    title = MULTISPACE_RE.sub(" ", " ".join(title_tokens)).strip(" -–_")
    return strip_junk_prefix(title), year


def _from_anitopy(raw: str) -> Optional[Extraction]:
    if not _HAS_ANITOPY:
        return None
    try:
        parsed = anitopy.parse(raw)
    except Exception:
        return None
    title = (parsed.get("anime_title") or "").strip()
    if not title:
        return None
    year = None
    if parsed.get("anime_year"):
        try:
            year = int(parsed["anime_year"])
        except (ValueError, TypeError):
            year = None
    ep = None
    raw_ep = parsed.get("episode_number")
    if isinstance(raw_ep, str) and raw_ep.isdigit():
        ep = int(raw_ep)
    elif isinstance(raw_ep, list) and raw_ep and str(raw_ep[0]).isdigit():
        ep = int(raw_ep[0])
    return Extraction(
        title=title,
        year=year,
        episode=EpisodeInfo(episode=ep),
        group=(parsed.get("release_group") or ""),
        anime_signal=True,
    )


def _from_guessit(raw: str) -> Optional[Extraction]:
    if not _HAS_GUESSIT:
        return None
    try:
        info = guessit(raw)
    except Exception:
        return None
    title = (info.get("title") or "").strip()
    if not title:
        return None
    year = info.get("year")
    season = info.get("season")
    episode = info.get("episode")
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    if isinstance(season, list):
        season = season[0] if season else None
    return Extraction(
        title=title,
        year=int(year) if isinstance(year, int) else None,
        episode=EpisodeInfo(season=season, episode=episode),
    )


def extract_from_filename(file_name: str) -> Extraction:
    """Extract a title from a Telegram media file_name (raw, unchangeable)."""
    raw = file_name or ""
    base = _strip_extension(raw)
    full_episode = parse_episode(base)
    anime_signal = bool(ANIME_HINT_RE.search(raw)) or _looks_like_anime_group(raw)

    # The series title is only ever the text BEFORE the first episode marker.
    # Everything after it (e.g. "finale", "Ozymandias", "the end") is the
    # episode title and must not pollute series identification/search. With a
    # marker at the very start (e.g. "S01E01-finale") there is no series title,
    # so this becomes an episode-only message that binds to the thread context.
    marker = episode_marker_start(base)
    if marker < 0:
        title_base = base
    elif marker > 0:
        title_base = base[:marker].strip(" -–_.")
    else:
        title_base = ""

    # Prefer the parser matching the strongest signal.
    chain = ([_from_anitopy, _from_guessit] if anime_signal
             else [_from_guessit, _from_anitopy])
    if title_base:
        for parser in chain:
            ex = parser(title_base)
            if ex and ex.has_title:
                ex.raw = raw
                ex.source_field = "file_name"
                ex.anime_signal = ex.anime_signal or anime_signal
                ex.episode = full_episode if full_episode.has_episode else parse_episode(base)
                ex.title = strip_junk_prefix(ex.title)
                ex.has_own_marker = marker >= 0
                if ex.has_title:
                    return ex

    title, year = _fallback_title(title_base) if title_base else ("", None)
    if year is None:
        # the year may sit before the marker even if the parser found no title
        ym = YEAR_RE.search(title_base or base)
        year = int(ym.group(1)) if ym else None
    return Extraction(
        title=title,
        year=year,
        episode=full_episode,
        anime_signal=anime_signal,
        source_field="file_name",
        has_own_marker=marker >= 0,
        raw=raw,
    )


def extract_from_text(text: str, source_field: str) -> Extraction:
    """Extract a title from caption/message_text (already line-limited).

    The same marker rule as for filenames applies: the series title is only the
    text BEFORE any episode marker. A caption like "E19 - Endlich Frieden" or
    "Episode 6 Staffel 1 ..." therefore yields NO series title (the rest is the
    episode title) and becomes an episode-only signal that binds to the thread.
    """
    raw = text or ""
    tags = _extract_hashtags(raw)
    # Remove hashtags from the title candidate (they are metadata, never titles).
    cleaned = HASHTAG_RE.sub(" ", raw).strip()
    anime_signal = bool(ANIME_HINT_RE.search(raw))

    full_episode = parse_episode(cleaned)
    marker = episode_marker_start(cleaned)
    if marker < 0:
        title_space = cleaned
    elif marker > 0:
        title_space = cleaned[:marker].strip(" -–_.")
    else:
        title_space = ""

    title, year = _fallback_title(title_space) if title_space else ("", None)
    # If the cleaned line is short and has no release noise, treat it as title.
    if not title and title_space and len(title_space.split()) <= 12:
        title = strip_junk_prefix(MULTISPACE_RE.sub(" ", title_space).strip(" -–_"))
    if year is None:
        ym = YEAR_RE.search(cleaned)  # the year may sit anywhere in the caption
        year = int(ym.group(1)) if ym else None

    return Extraction(
        title=title,
        year=year,
        tags=tags,
        episode=full_episode,
        anime_signal=anime_signal,
        source_field=source_field,
        has_own_marker=marker >= 0,
        raw=raw,
    )


def _looks_like_anime_group(raw: str) -> bool:
    m = ANIME_GROUP_RE.match(raw)
    if not m:
        return False
    inner = m.group(1).strip().lower()
    if YEAR_RE.fullmatch(inner):
        return False
    if inner in RELEASE_TOKENS:
        return False
    return True
