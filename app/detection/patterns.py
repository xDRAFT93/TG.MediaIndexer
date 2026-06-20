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

# Audio container extensions -> a file with one of these is treated as an
# audiobook in this catalog (music is not indexed here).
AUDIO_EXTENSIONS = {
    "mp3", "m4a", "m4b", "aac", "flac", "ogg", "oga", "opus", "wma", "m4p",
    "alac", "ape", "wav",
}

# German (and a few English) audiobook signal words. A match anywhere in the
# file name or post text marks the item as an audiobook even without an audio
# extension (e.g. a multi-file release described only in the caption).
AUDIOBOOK_KEYWORDS_RE = re.compile(
    r"(?i)\b(h[oö]rbuch|h[oö]rspiel|ungek[uü]rzt|gek[uü]rzt|lesung|gelesen\s+von|"
    r"gesprochen\s+von|vorgelesen|audiobook|audio\s?book|h[oö]rbuchfassung)\b"
)

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
    # S01E01 / S1.E1 / S01 E01, also when glued to the title ("LuciferS04E09").
    re.compile(r"(?i)S(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,3})(?![0-9])"),
    # German short form S1F1 / S01F05 (Staffel x Folge y).
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._-]*F(?P<episode>\d{1,3})(?![0-9])"),
    # 1x05 / 09x01 / 9X01 (case-insensitive x), not preceded by a digit/p/x
    re.compile(r"(?<![0-9pPxX])\b(?P<season>\d{1,2})[xX](?P<episode>\d{1,3})\b"),
    # season 1 episode 5 / staffel 1 folge 5 / season 1 ep 5
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})[ ._-]+"
               r"(?:episode|folge|ep|e)[ ._-]*(?P<episode>\d{1,3})\b"),
    # episode 6 staffel 1  (marker order reversed)
    re.compile(r"(?i)\b(?:episode|folge)[ ._-]*(?P<episode>\d{1,3})[ ._-]+"
               r"(?:season|staffel)[ ._-]*(?P<season>\d{1,2})\b"),
    # S1-05 (season then bare episode after dash)
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._]*[-–][ ._]*(?P<episode>\d{1,3})(?![0-9])\b"),
    # Leading "04.01" / "04x01" style = Season 04, Episode 01 (episode is 2 digits
    # so a plain decimal like "1.5" is not captured). Anchored to the start.
    re.compile(r"^(?P<season>\d{1,2})[._](?P<episode>\d{2})(?=[ ._-]|$)"),
]

EPISODE_ONLY_RES: list[re.Pattern] = [
    # episode/folge/ep/cap[itulo] + number (1-3 digits; a 4-digit number is a year)
    re.compile(r"(?i)\b(?:episode|folge|ep|cap(?:itulo)?)[ ._#-]*(?P<episode>\d{1,3})(?![0-9])\b"),
    # bare E16 / E016 (not part of a word, not a resolution like E2160p)
    re.compile(r"(?i)(?<![a-z0-9])E(?P<episode>\d{1,3})(?![0-9pP])\b"),
    # part markers TitelT01 / T01 / t02  (Teil/part), 1-2 digits
    re.compile(r"(?i)(?:(?<=[a-zäöü])|(?<![a-z0-9]))T(?P<episode>\d{1,2})(?![0-9])\b"),
    # #16
    re.compile(r"(?<![\w])#[ ]?(?P<episode>\d{1,3})(?![0-9])\b"),
    # Anime "- 05" / "– 05" / "] 05" after a title (1-3 digits; never a 4-digit year)
    re.compile(r"(?:^|[\]\)\s])[-–][ ]?(?P<episode>\d{1,3})(?:v\d)?(?=[ \[\(.]|$)"),
    # Leading bare episode number in list style: "16 - Title" / "16. Title".
    re.compile(r"^[\[\(]?(?P<episode>\d{1,3})[\]\)]?[ ._]*[-–.][ ._]+(?=\S)"),
    # Leading "10.1" / "01.2" = episode 10 / 1 with a SUB-part (one digit after the
    # dot). Distinct from the SxxEyy "04.01" form, which has two digits after the
    # dot and is matched earlier. The integer part is the episode number.
    re.compile(r"^(?P<episode>\d{1,3})\.\d(?!\d)"),
    # Leading "1a" / "2b" / "10c" = episode number with a part letter. Anchored to
    # the end so a title like "2B Movie" is NOT misread as episode 2.
    re.compile(r"(?i)^(?P<episode>\d{1,3})[a-e]$"),
    # Leading "01_Titel" / "10_Titel" = episode number then underscore then title.
    re.compile(r"^(?P<episode>\d{1,3})_(?=\D)"),
    # Disc/opening/ending/special markers: bd1, ed2, op1, sp3, ova2 (treated as
    # episode numbers so they bind to the series instead of being searched as a
    # bogus title).
    re.compile(r"(?i)^(?:bd|ed|op|sp|ova|oad|nced|ncop)[ ._-]?(?P<episode>\d{1,3})(?![0-9])"),
    # Leading ZERO-PADDED number ("044 Titel", "007 ..."). A leading zero marks a
    # track/episode number, so this is safe where a bare "300" is not. int()
    # collapses the padding ("044" -> 44).
    re.compile(r"^(?P<episode>0\d{1,3})(?=[ ._/-]|$)"),
    # Weakest: a 1-2 digit number, space(s), then a word — "4 Ausgebrannt ...".
    # Requires a real space so dotted names ("300.2006", "24.mkv") are unaffected;
    # capped at 2 digits so "300"/"1917" stay films. In a non-series thread an
    # accidental match merely lands in pending; in a series thread it binds.
    re.compile(r"^(?P<episode>\d{1,2})[ ]+(?=[^\d\s])"),
]

SEASON_ONLY_RES: list[re.Pattern] = [
    re.compile(r"(?i)\b(?:season|staffel)[ ._-]*(?P<season>\d{1,2})\b"),
    re.compile(r"(?i)\bS(?P<season>\d{1,2})(?![0-9eE])\b"),
]

# Used to scrub episode markers out of a title candidate.
EP_SCRUB_RES: list[re.Pattern] = SEASON_EPISODE_RES + EPISODE_ONLY_RES + SEASON_ONLY_RES

# Downloader / ripper / site noise that gets prepended to titles. "Strong" names
# are site/ripper brands that are never part of a real title, so they are removed
# even when leading. "Weak" words (watch/online/...) are only removed once a
# strong token has already been dropped (so real titles like "Watch Dogs" or
# "The Ting" survive). Leading glue ("is") is dropped only after a strong drop.
JUNK_STRONG_PREFIXES = (
    "y2mate", "ssyoutube", "savefrom", "vivo", "9xmovies", "1tamilmv", "tamilmv",
    "filmywap", "khatrimaza", "moviesflix", "bolly4u", "worldfree4u", "mkvcinemas",
    "katmoviehd", "ssrmovies", "uwatchfree", "pagalmovies",
)
JUNK_STRONG_EXACT = {"www", "com", "net", "org"}
JUNK_WEAK_TOKENS = {"watch", "online", "download", "downloaden", "free", "fullhd", "stream"}
JUNK_LEADING_GLUE = {"is"}

SEPARATOR_RE = re.compile(r"[._]+")
MULTISPACE_RE = re.compile(r"\s+")
