"""Classifier.

Combines the per-field extractions under the strict title priority:
    media.file_name  >  caption  >  message_text  >  (thread context, elsewhere)

and decides the media type (film / series / anime) with a confidence score.
Hashtags are tags, never titles. Episode-only messages are flagged for binding
to the thread's active media (handled by the context manager).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from .episodes import EpisodeInfo
from ..storage.models import MediaType
from .extractor import Extraction, extract_from_filename, extract_from_text
from .patterns import AUDIO_EXTENSIONS, AUDIOBOOK_KEYWORDS_RE


@dataclass
class Detection:
    has_title: bool = False
    only_episode: bool = False
    title: str = ""
    year: Optional[int] = None
    media_type: MediaType = MediaType.FILM
    episode: EpisodeInfo = field(default_factory=EpisodeInfo)
    tags: list[str] = field(default_factory=list)
    anime_signal: bool = False
    audiobook_signal: bool = False
    confidence: float = 0.0
    title_source: str = ""
    # All distinct title candidates (file name, caption, post text) in priority
    # order, so resolution can fall back to the post text when the file-name
    # title does not match a provider.
    search_titles: list[str] = field(default_factory=list)
    # Audiobook hints parsed from the file name ("Autor 1, Autor 2 - Titel",
    # "… Band 3") used to score and verify the provider match.
    authors: list[str] = field(default_factory=list)
    volume: Optional[int] = None
    series: str = ""

    @property
    def provider_query(self) -> str:
        if self.year:
            return f"{self.title} {self.year}".strip()
        return self.title.strip()


_TITLE_SOURCE_WEIGHT = {
    "file_name": 0.90,
    "caption": 0.70,
    "message_text": 0.55,
}

# Mathematical sans-serif/serif bold & italic digits also fold via NFKC; this map
# covers a few stylised forms NFKC misses (circled/parenthesised are handled by
# NFKC, so only a safety net here).
_DIGIT_FALLBACK = {
    "\U0001D7CE": "0", "\U0001D7CF": "1", "\U0001D7D0": "2", "\U0001D7D1": "3",
}

# Emoji / pictographic / decorative symbol ranges. Channels wrap titles in these
# ("🔥 Title 🔥", "⭐ Title ⭐", "▶ Title ◀"), which breaks both grouping (each
# decoration differs) and provider search. They are never part of a real title.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji blocks (symbols/emoticons/transport/supplemental)
    "\U00002600-\U000027BF"   # misc symbols + dingbats (★ ☎ ✂ ✦ ➤ …)
    "\U00002190-\U000021FF"   # arrows
    "\U00002300-\U000023FF"   # misc technical (⏳ ⎪ ⌛ …)
    "\U000025A0-\U000025FF"   # geometric shapes (■ ◆ ▶ ● …)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows (⬛ ⭐ ➡ …)
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U00002066-\U0000206F"   # bidi/format controls
    "\U0000200B-\U0000200F"   # zero-width spaces/joiner, marks
    "\U000020E3\U00002022\U0000FE0F\U00002028\U00002029"
    "\U00003010\U00003011\U0000300A\U0000300B\U00003008\U00003009"  # 【】《》〈〉
    "\U0000300C\U0000300D\U0000300E\U0000300F"                      # 「」『』
    "\U00003016\U00003017\U00003014\U00003015"                      # 〖〗〔〕
    "❮❯❰❱➤➢▷◁"
    "]+",
    flags=re.UNICODE,
)


def _normalize_unicode(s: str) -> str:
    """Fold stylised Unicode to plain ASCII-ish text and drop decorative emoji.

    NFKC compatibility normalisation converts the mathematical alphanumeric
    symbols Telegram channels love (bold/italic/sans-serif letters and digits)
    and modifier letters (ᴴᴰ) to their plain equivalents; decorative emoji and
    pictographic symbols are then removed so the regex-based marker/title
    detection — and grouping — work on styled posts. Newlines are preserved so
    the line-based title extraction still sees the post's structure.
    """
    if not s:
        return s
    s = unicodedata.normalize("NFKC", s)
    s = _EMOJI_RE.sub(" ", s)
    if any(ch in _DIGIT_FALLBACK for ch in s):
        s = "".join(_DIGIT_FALLBACK.get(ch, ch) for ch in s)
    s = re.sub(r"[ \t]+", " ", s)  # collapse horizontal whitespace, keep newlines
    return s


# Decorative punctuation that should never sit at a title edge (separators,
# brackets-as-decoration, quotes …). Letters, digits and round parentheses are
# kept so "(2024)"-style suffixes survive.
_EDGE_STRIP = set("|-–—:;·•.,/\\_«»‹›*~^+=#@!?\"'`“”„‚‘’…⎪┃▬<>[]{}"
                  "『』「」【】〖〗〔〕《》〈〉｜～❮❯➤➢▷◁❰❱")

# Narrow set of quality/resolution markers that are decorative when they LEAD a
# title (often left behind by a 【HD】/【4K】 prefix tag). Kept deliberately small
# so a real title like "WEB of Lies" is never truncated.
_LEADING_QUALITY_RE = re.compile(
    r"^(?:\d{3,4}p|[2-9]k|uhd|fhd|qhd|hd|sd|hdr\d*)\b[ ._-]*",
    re.IGNORECASE,
)


def _clean_title_edges(title: str) -> str:
    """Strip whitespace, decorative punctuation and any symbol characters left
    dangling at a title's edges, plus a leading quality marker, e.g.
    "🔥 Eternal You 🔥" -> "Eternal You", "Peaky Blinders |" -> "Peaky Blinders",
    "HD Peaky Blinders" (from "【HD】 Peaky Blinders") -> "Peaky Blinders"."""
    chars = list(title or "")

    def junk(ch: str) -> bool:
        return ch.isspace() or ch in _EDGE_STRIP or unicodedata.category(ch)[0] == "S"

    while chars and junk(chars[0]):
        chars.pop(0)
    while chars and junk(chars[-1]):
        chars.pop()
    out = "".join(chars).strip()
    # Drop a leading quality token, then re-strip any edge junk it exposed.
    new = _LEADING_QUALITY_RE.sub("", out)
    if new != out:
        new = new.strip(" ._-").strip()
        if new:  # never strip the title away entirely
            out = new
    return out


def _bare_number_episode(title: str) -> Optional[int]:
    """If the whole title is just a number ("01", "100"), return it as an episode
    number. Such "titles" otherwise hit the providers and produce a wrong match
    and a bogus standalone entry. 4-digit years stay titles (a film could be a
    year), everything else 1-4 digits is treated as an episode number."""
    t = (title or "").strip()
    if not re.fullmatch(r"\d{1,4}", t):
        return None
    n = int(t)
    if len(t) == 4 and 1900 <= n <= 2099:
        return None
    return n if n > 0 else None


_ARTICLES = {"der", "die", "das", "ein", "eine", "the", "a", "an", "les", "la",
             "le", "el", "los", "den", "dem", "des"}
_AUTHOR_HYPHEN_RE = re.compile(r"\s+[-–—]\s+")
_VOLUME_RE = re.compile(r"\b(?:band|teil|vol|volume|buch|book)\.?\s*(\d{1,3})\b", re.I)
_VOLUME_SUFFIX_RE = re.compile(
    r"\s*[\(\[]?\s*(?:band|teil|vol|volume|buch|book)\.?\s*\d{1,3}\s*[\)\]]?\s*$", re.I)
# Trailing audiobook qualifiers to drop from a parsed book title.
_AB_QUALIFIER_RE = re.compile(
    r"\s*[\(\[]?\s*(?:ungek[üu]rzt|gek[üu]rzt|gekuerzt|h[öo]rbuch|h[öo]rspiel|"
    r"lesung|autorenlesung|audiobook|komplett|vollst[äa]ndig)\s*[\)\]]?\s*$",
    re.IGNORECASE,
)


def _looks_like_author_list(s: str) -> bool:
    """Whether the part before a ' - ' is plausibly one or more person names
    (capitalised, no digits, short, not starting with an article). This keeps a
    'Series - Volume' string from being misread as 'Author - Title'."""
    s = (s or "").strip()
    if not s or any(ch.isdigit() for ch in s):
        return False
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return False
    for p in parts:
        toks = p.split()
        if not (1 <= len(toks) <= 4):
            return False
        if toks[0].lower() in _ARTICLES:
            return False
        if not toks[0][:1].isupper():
            return False
    return True


def _parse_author_title(text: str) -> tuple[list[str], str]:
    authors, title, _series, _vol = _parse_audiobook_meta(text)
    return authors, title


_SERIES_KW_RE = re.compile(
    r"\b(reihe|zyklus|trilogie|tetralogie|serie|saga|sammelband|chronik|chroniken|edition)\b",
    re.IGNORECASE)
_SERIES_STRIP_RE = re.compile(
    r"\b(?:band|teil|vol|volume|buch|book|sammelband|folge|nr)\.?\s*\d{1,3}\b", re.IGNORECASE)


def _series_segment(seg: str) -> tuple[Optional[str], Optional[int]]:
    """If a ' - ' segment denotes a series/volume ('Sehnsuchtswald-Reihe 01',
    'Marseille-Trilogie, Band 3'), return (series_name, volume); else (None, None)."""
    has_kw = bool(_SERIES_KW_RE.search(seg))
    vol = _parse_volume(seg)
    m = re.search(r"\b(\d{1,3})\b\s*$", seg.strip())
    trailing = int(m.group(1)) if m else None
    if not has_kw and vol is None:
        return None, None
    volume = vol if vol is not None else trailing
    name = _AB_QUALIFIER_RE.sub("", seg)
    name = _SERIES_STRIP_RE.sub("", name)
    name = re.sub(r"\b\d{1,3}\b\s*$", "", name)
    name = re.sub(r"[,;]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -,")
    return name, volume


def _parse_audiobook_meta(text: str) -> tuple[list[str], str, str, Optional[int]]:
    """Parse 'Author[, Author] - [Series Vol -] Title [- …] (Qualifier)' into
    (authors, title, series, volume). Handles comma-separated authors and
    hyphen-separated multiple authors ('Author1 - Author2 - Title'), and keeps a
    'Series - Volume' string from being misread as an author/title."""
    segs = [s.strip() for s in _AUTHOR_HYPHEN_RE.split(text or "") if s.strip()]
    if len(segs) < 2:
        return [], _clean_book_title(text or ""), "", _parse_volume(text)

    authors: list[str] = []
    i = 0
    while i < len(segs) - 1:
        seg = segs[i]
        if not _looks_like_author_list(seg):
            break
        # A 2nd+ leading author must be a FULL name (>=2 words per part) so a
        # single-word title ("Solea") is not swallowed as an author.
        if i >= 1 and not all(len(p.split()) >= 2 for p in seg.split(",") if p.strip()):
            break
        authors.extend(p.strip() for p in seg.split(",") if p.strip())
        i += 1
    if not authors:
        return [], _clean_book_title(text or ""), "", _parse_volume(text)

    rest = segs[i:]
    series, volume = "", None
    title_parts: list[str] = []
    for seg in rest:
        s_name, s_vol = _series_segment(seg)
        if s_name is not None:
            if not series and s_name:
                series = s_name
            if volume is None and s_vol is not None:
                volume = s_vol
        else:
            title_parts.append(seg)
    title = _clean_book_title(title_parts[0]) if title_parts else _clean_book_title(rest[0])
    if volume is None:
        volume = _parse_volume(text)
    return authors, title, series, volume


def _parse_volume(text: str) -> Optional[int]:
    m = _VOLUME_RE.search(text or "")
    return int(m.group(1)) if m else None


def _clean_book_title(title: str) -> str:
    """Edge-clean a book title and drop trailing audiobook qualifiers and a
    trailing volume marker ('… Band 1')."""
    t = _clean_title_edges(title or "")
    prev = None
    while t and t != prev:
        prev = t
        t = _AB_QUALIFIER_RE.sub("", t)
        t = _VOLUME_SUFFIX_RE.sub("", t)
        t = _clean_title_edges(t)
    return t


def classify(file_name: str, caption: str, message_text: str) -> Detection:
    # Fold "fancy" Unicode (mathematical bold/italic, modifier letters, full-width,
    # bold digits …) to plain ASCII so markers and titles in styled Telegram posts
    # are recognised, e.g. "𝗦𝗲𝗮𝘀𝗼𝗻 𝟮 ᴴᴰ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲 𝟱" -> "Season 2 HD Episode 5".
    file_name = _normalize_unicode(file_name)
    caption = _normalize_unicode(caption)
    message_text = _normalize_unicode(message_text)

    extractions: list[Extraction] = []
    if file_name:
        extractions.append(extract_from_filename(file_name))
    if caption:
        extractions.append(extract_from_text(caption, "caption"))
    if message_text:
        extractions.append(extract_from_text(message_text, "message_text"))

    tags: list[str] = []
    anime_signal = False
    for ex in extractions:
        for t in ex.tags:
            if t not in tags:
                tags.append(t)
        anime_signal = anime_signal or ex.anime_signal

    # Episode: prefer a source with full season+episode, then any episode, then a
    # season-only signal — scanning sources in priority order (file/caption/text).
    episode = EpisodeInfo()
    for ex in extractions:
        if ex.episode.has_episode and ex.episode.season is not None:
            episode = ex.episode
            break
    if not episode.has_episode:
        for ex in extractions:
            if ex.episode.has_episode:
                episode = ex.episode
                break
    if not episode.has_any:
        for ex in extractions:
            if ex.episode.has_any:
                episode = ex.episode
                break

    anime_by_tag = any(t == "anime" for t in tags)
    anime_signal = anime_signal or anime_by_tag

    # Audiobook signal: an audio file extension, or an audiobook keyword anywhere
    # in the file name / caption / post text. Audio always wins over the
    # film/series/anime guess because those are video formats.
    audiobook_signal = _is_audiobook(file_name, caption, message_text)

    has_marker = any(ex.has_own_marker for ex in extractions)

    # Series-title selection. The decisive rule: when an episode marker exists
    # ANYWHERE, the series title is the text BEFORE the marker, taken from the
    # highest-priority source that itself carried the marker. A source whose only
    # "title" is really the episode title (no marker of its own) is NOT used as a
    # series name. If no marker-bearing source yields a title, this is an
    # episode-only message that must bind to the thread's series.
    if has_marker:
        chosen = next((ex for ex in extractions
                       if ex.has_own_marker and ex.has_title), None)
    else:
        chosen = next((ex for ex in extractions if ex.has_title), None)

    # Year: from the chosen source, else from any source that carries one.
    year = chosen.year if chosen else None
    if year is None:
        for ex in extractions:
            if ex.year:
                year = ex.year
                break

    # A file whose entire title is just a number ("01", "100") is an episode
    # number, not a searchable title. Re-route it to episode-only binding so it
    # joins the thread's series instead of producing a wrong provider match.
    if chosen is not None and not episode.has_episode:
        bare = _bare_number_episode(chosen.title)
        if bare is not None:
            episode = EpisodeInfo(season=episode.season, episode=bare)
            chosen = None

    # Bare-number FILE NAME ("1.mp3", "100.m4b"): the extractor sometimes drops a
    # lone number (it can look like an audio-channel token such as 2.0/5.1), so
    # derive it straight from the file name when no title/episode was found. Such
    # numbered files are parts that must bind to the thread's active media.
    if not episode.has_episode and (chosen is None or not (chosen.title or "").strip()):
        fn_base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", file_name or "").strip()
        bare_fn = _bare_number_episode(fn_base)
        if bare_fn is not None:
            episode = EpisodeInfo(season=episode.season, episode=bare_fn)
            chosen = None

    if chosen is None:
        # No usable series title. With an episode -> bind to the thread context.
        # An audiobook part ("Teil 2.mp3") binds to the active audiobook as an
        # extra release (handled downstream), never as a TV episode.
        if episode.has_episode and not audiobook_signal:
            return Detection(
                has_title=False,
                only_episode=True,
                episode=episode,
                year=year,
                tags=tags,
                anime_signal=anime_signal,
                audiobook_signal=audiobook_signal,
                confidence=0.5,
            )
        if audiobook_signal and episode.has_episode:
            # Audiobook part with no book title in this file -> bind as a part.
            return Detection(
                has_title=False,
                only_episode=True,
                episode=episode,
                year=year,
                tags=tags,
                audiobook_signal=True,
                media_type=MediaType.AUDIOBOOK,
                confidence=0.45,
            )
        return Detection(has_title=False, only_episode=False, year=year, tags=tags,
                         anime_signal=anime_signal, audiobook_signal=audiobook_signal,
                         confidence=0.0)

    media_type, type_conf = _decide_type(chosen, episode, anime_signal, audiobook_signal)

    base = _TITLE_SOURCE_WEIGHT.get(chosen.source_field, 0.5)
    confidence = base + (0.08 if year else 0.0)
    confidence = min(1.0, confidence * (0.6 + 0.4 * type_conf))

    # Collect every distinct title candidate (chosen first), so resolution can
    # fall back from a cryptic file name ("tmsf-eternalyou") to the real title in
    # the post text ("eternal you - vom ende der endlichkeit").
    candidates: list[str] = []
    for ex in [chosen] + [e for e in extractions if e is not chosen]:
        t = _clean_title_edges(ex.title or "")
        if t and t.lower() not in {c.lower() for c in candidates}:
            candidates.append(t)
    # Display title: the most descriptive candidate (more real words wins). This
    # keeps a clean post-text title over a junky file-name token even when the
    # entry stays unresolved.
    display = _best_display_title(candidates) if candidates else chosen.title

    # Audiobook author/title parsing: "Autor 1, Autor 2 - Titel" or
    # "Autor - Serie Band NN - Titel" -> authors + clean book title + series +
    # volume. Tried on the post text first (it carries the ' - ' structure), then
    # the file name. The book title becomes the primary search candidate; an
    # "authors + title" query is added so Audible's keyword search resolves an
    # ASIN even from a cryptic file name.
    authors_parsed: list[str] = []
    series_parsed = ""
    volume_parsed: Optional[int] = None
    if audiobook_signal:
        fn_base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", file_name or "")
        sources = [caption or "", (message_text or "").split("\n", 1)[0], fn_base]
        for src in sources:
            a, t, s, v = _parse_audiobook_meta(src)
            if a:
                authors_parsed, series_parsed, volume_parsed = a, s, v
                book_title = t or display
                if book_title:
                    display = book_title
                    queries = [book_title, f"{' '.join(a)} {book_title}".strip()]
                    lowers = {c.lower() for c in candidates}
                    for q in reversed(queries):
                        if q and q.lower() not in lowers:
                            candidates.insert(0, q)
                            lowers.add(q.lower())
                break
        if volume_parsed is None:
            for src in sources:
                volume_parsed = _parse_volume(src)
                if volume_parsed:
                    break

    return Detection(
        has_title=True,
        only_episode=False,
        title=display or chosen.title,
        year=year,
        media_type=media_type,
        episode=episode,
        tags=tags,
        anime_signal=anime_signal,
        audiobook_signal=audiobook_signal,
        confidence=round(confidence, 3),
        title_source=chosen.source_field,
        search_titles=candidates,
        authors=authors_parsed,
        volume=volume_parsed,
        series=series_parsed,
    )


def _word_count(title: str) -> int:
    """Number of whitespace-separated tokens that contain a letter."""
    return sum(1 for tok in (title or "").split() if any(ch.isalpha() for ch in tok))


def _best_display_title(candidates: list[str]) -> str:
    """Pick the most title-like candidate. If the file-name title is a single
    slug-like token ("tmsf-eternalyou") but the post text offers a real
    multi-word title, prefer the latter; otherwise keep the file-name title."""
    chosen = candidates[0]
    if _word_count(chosen) >= 2:
        return chosen
    for c in candidates[1:]:
        if _word_count(c) >= 2:
            return c
    return chosen


def _decide_type(chosen: Extraction, episode: EpisodeInfo,
                 anime_signal: bool, audiobook_signal: bool) -> tuple[MediaType, float]:
    if audiobook_signal:
        return MediaType.AUDIOBOOK, 0.85
    if anime_signal:
        return MediaType.ANIME, 0.85
    if episode.has_episode or episode.season is not None:
        return MediaType.SERIES, 0.75
    return MediaType.FILM, 0.6


def _is_audiobook(file_name: str, caption: str, message_text: str) -> bool:
    """An audio file extension or any audiobook keyword in the available text."""
    fn = (file_name or "").lower()
    if "." in fn:
        ext = fn.rsplit(".", 1)[-1]
        if ext in AUDIO_EXTENSIONS:
            return True
    blob = " ".join([file_name or "", caption or "", message_text or ""])
    return bool(AUDIOBOOK_KEYWORDS_RE.search(blob))
