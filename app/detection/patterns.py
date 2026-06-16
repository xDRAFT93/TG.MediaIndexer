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
# Each captures named groups 'season' and/or 'episode'. These also drive title
# isolation: the series title is only ever the text BEFORE the earliest marker.
SEASON_EPISODE_RES: list[re.Pattern] = [
    # S01E01 / S1.E1 / S01 E01 (any run of separators)
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,3})(?![0-9])\b"),
    # 1x05 / 09x01 / 9X01 (case-insensitive x), not preceded by a digit/p/x
    re.compile(r"(?<![0-9pPxX])\b(?P<season>\d{1,2})[xX](?P<episode>\d{1,3})\b"),
    # season 1 episode 5 / staffel 1 folge 5 / season 1 ep 5
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})[ ._-]+"
               r"(?:episode|folge|ep|e)[ ._-]*(?P<episode>\d{1,3})\b"),
    # S01.E01 already covered; S1-05 (season then bare episode after dash)
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._]*[-–][ ._]*(?P<episode>\d{1,3})(?![0-9])\b"),
]

EPISODE_ONLY_RES: list[re.Pattern] = [
    # episode/folge/ep/cap[itulo]/E + number, optional #/._- separators
    re.compile(r"(?i)\b(?:episode|folge|ep|cap(?:itulo)?)[ ._#-]*(?P<episode>\d{1,4})\b"),
    # bare E16 / E016 (not part of a word, not a resolution like E2160p)
    re.compile(r"(?i)(?<![a-z0-9])E(?P<episode>\d{1,3})(?![0-9pP])\b"),
    # #16
    re.compile(r"(?<![\w])#[ ]?(?P<episode>\d{1,4})\b"),
    # Anime "- 05" / "– 05" / "] 05" after a title or bracket.
    re.compile(r"(?:^|[\]\)\s])[-–][ ]?(?P<episode>\d{1,4})(?:v\d)?(?=[ \[\(.]|$)"),
    # Leading bare episode number in episode-list style: "16 - Title" / "16. Title"
    # / "16 _ Title". 1-3 digits only (excludes 4-digit years); the spaced dash /
    # dot separator is the episode-list signal. Anchored to the start so it never
    # fires mid-name (e.g. inside "Doctor Who 2005 ...").
    re.compile(r"^[\[\(]?(?P<episode>\d{1,3})[\]\)]?[ ._]*[-–.][ ._]+(?=\S)"),
]

SEASON_ONLY_RES: list[re.Pattern] = [
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})\b"),
    re.compile(r"(?i)\bS(?P<season>\d{1,2})(?![0-9eE])\b"),
]

# Used to scrub episode markers out of a title candidate.
EP_SCRUB_RES: list[re.Pattern] = SEASON_EPISODE_RES + EPISODE_ONLY_RES + SEASON_ONLY_RES

SEPARATOR_RE = re.compile(r"[._]+")
MULTISPACE_RE = re.compile(r"\s+")
