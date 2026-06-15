"""Static, deterministic pattern tables for detection.

The system's "learning" is explicit: these tables plus the DB-backed alias
table. There is no implicit behaviour change. Tune detection by editing these
tables or by adding aliases in the ``patterns`` collection.
"""
from __future__ import annotations

import re

# File extensions that mark a media file name (stripped before title extraction).
VIDEO_EXTENSIONS = {
    "mkv", "mp4", "avi", "mov", "wmv", "flv", "m4v", "ts", "m2ts", "mpg",
    "mpeg", "webm", "ogm", "vob", "iso", "rmvb", "divx",
}

# Pure noise tokens removed from any title candidate (lowercase compare).
RELEASE_TOKENS = {
    # resolution / quality
    "480p", "576p", "720p", "1080p", "1440p", "2160p", "4k", "8k", "uhd", "hd",
    "fhd", "qhd", "sd", "hdr", "hdr10", "dv", "dolby", "vision", "sdr", "10bit",
    "8bit", "hi10p", "hi10",
    # source
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "web-dl", "webdl",
    "webrip", "web", "hdtv", "dvdrip", "dvd", "hdrip", "cam", "ts", "tc", "r5",
    "dsr", "pdtv", "amzn", "nf", "dsnp", "atvp", "hmax", "itunes",
    # codecs / audio
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "vp9", "av1",
    "aac", "aac2", "ac3", "eac3", "dd", "ddp", "dd5", "dd51", "ddp5", "dts",
    "dtshd", "truehd", "atmos", "flac", "mp3", "opus", "2", "0", "1", "5", "6",
    "7", "ch",
    # language / dub
    "dl", "ml", "dual", "multi", "german", "deutsch", "ger", "english", "eng",
    "en", "japanese", "jpn", "jap", "italian", "ita", "french", "fra", "spanish",
    "esp", "subbed", "dubbed", "subs", "sub", "dub", "ond", "synced", "korean",
    # misc tags
    "proper", "repack", "internal", "limited", "extended", "unrated", "directors",
    "cut", "theatrical", "imax", "remastered", "complete", "uncut", "readnfo",
    "retail", "custom", "rerip", "untouched",
}

HASHTAG_RE = re.compile(r"(?:^|\s)#(\w+)", re.UNICODE)

# A 4-digit year, usually in parentheses/brackets, between 1900 and 2099.
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

# Anime fansub group at start, e.g. [SubsPlease] ...
ANIME_GROUP_RE = re.compile(r"^\s*\[([^\]]+)\]")

# Trailing release group, e.g. ...-GROUP
TRAILING_GROUP_RE = re.compile(r"[-–][A-Za-z0-9]{2,}$")

# Anything in brackets/braces (release metadata in anime names).
BRACKET_RE = re.compile(r"[\[\({][^\]\)}]*[\]\)}]")

# Markers that strongly suggest anime fansub naming.
ANIME_HINT_RE = re.compile(
    r"\[(?:SubsPlease|Erai-raws|HorribleSubs|EMBER|Judas|ASW|Anime Time|"
    r"Commie|GJM|Cleo|Golumpa|Ohys-Raws|Nyaa)[^\]]*\]",
    re.IGNORECASE,
)

# --- Season/episode patterns (ordered; first match wins) ---------------------
# Each captures named groups 'season' and/or 'episode'.
SEASON_EPISODE_RES: list[re.Pattern] = [
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._-]?E(?P<episode>\d{1,3})\b"),
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})[ ._-]+"
               r"(?:episode|folge|ep|e)[ ._-]*(?P<episode>\d{1,3})\b"),
    re.compile(r"(?<![0-9pPxX])\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b"),
]

EPISODE_ONLY_RES: list[re.Pattern] = [
    re.compile(r"(?i)\b(?:episode|folge|ep|cap(?:itulo)?)[ ._#-]*(?P<episode>\d{1,4})\b"),
    re.compile(r"(?i)(?<![a-z0-9])E(?P<episode>\d{1,3})(?![0-9pP])\b"),
    # Anime " - 05" / " – 05" after a title.
    re.compile(r"(?:^|[\]\)\s])[-–][ ]?(?P<episode>\d{1,4})(?:v\d)?(?=[ \[\(]|$)"),
]

SEASON_ONLY_RES: list[re.Pattern] = [
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})\b"),
    re.compile(r"(?i)\bS(?P<season>\d{1,2})(?![0-9eE])\b"),
]

# Used to scrub episode markers out of a title candidate.
EP_SCRUB_RES: list[re.Pattern] = SEASON_EPISODE_RES + EPISODE_ONLY_RES + SEASON_ONLY_RES

SEPARATOR_RE = re.compile(r"[._]+")
MULTISPACE_RE = re.compile(r"\s+")
