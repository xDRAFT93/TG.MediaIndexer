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
    raw: str = ""

    @property
    def has_title(self) -> bool:
        return bool(self.title.strip())


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
    return title, year


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
        raw=raw,
    )


def extract_from_text(text: str, source_field: str) -> Extraction:
    """Extract a title from caption/message_text (already line-limited)."""
    raw = text or ""
    tags = _extract_hashtags(raw)
    # Remove hashtags from the title candidate (they are metadata, never titles).
    cleaned = HASHTAG_RE.sub(" ", raw).strip()
    anime_signal = bool(ANIME_HINT_RE.search(raw))

    title, year = _fallback_title(cleaned)
    # If the cleaned line is short and has no release noise, treat it as title.
    if not title and cleaned and len(cleaned.split()) <= 12:
        title = MULTISPACE_RE.sub(" ", cleaned).strip(" -–_")

    return Extraction(
        title=title,
        year=year,
        tags=tags,
        episode=parse_episode(raw),
        anime_signal=anime_signal,
        source_field=source_field,
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
