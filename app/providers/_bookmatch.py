"""Precise title matching for audiobooks.

The hard problem with books is that a query is usually "Author - Title" and a
provider returns many works by that author. Scoring the raw query against a
result title lets the author dominate, so the WRONG book by the right author
slips through (the reported failure). The fix: remove the result's author tokens
from the query first, then score what remains against the result title. The
author thus stops inflating the score; the title decides the match.
"""
from __future__ import annotations

import re
from typing import Optional

from ..detection.confidence import title_similarity

_SPLIT = re.compile(r"[^0-9A-Za-zÀ-ÿ]+")


def strip_authors(query: str, authors: Optional[list]) -> str:
    """Remove author-name tokens (>=2 chars) from the query, wherever they sit,
    so only the book-title portion remains for comparison."""
    q = query or ""
    for a in authors or []:
        for tok in _SPLIT.split(a):
            if len(tok) >= 2:
                q = re.sub(rf"\b{re.escape(tok)}\b", " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def book_title_score(query: str, title: str, authors: Optional[list] = None,
                     original_title: str = "") -> float:
    """0..100 similarity of the query's TITLE part (author removed) to a result
    title. Returns 0 when nothing but the author remains (we then cannot tell
    which of the author's books was meant)."""
    q_title = strip_authors(query, authors)
    if not q_title or len(q_title) < 2:
        return 0.0
    score = title_similarity(q_title, title or "")
    if original_title:
        score = max(score, title_similarity(q_title, original_title))
    return score


def author_present(query: str, authors: Optional[list]) -> bool:
    """Whether the query plausibly mentions one of the result's authors."""
    ql = (query or "").lower()
    for a in authors or []:
        if a and (a.lower() in ql or title_similarity(query, a) >= 70):
            return True
    return False


def select_best(query: str, candidates: list) -> tuple[Optional[dict], float]:
    """Pick the candidate whose title best matches the query.

    ``candidates`` is a list of dicts: {'title', 'authors', 'original_title',
    'raw'}. Returns (best_candidate, score). An author-confirmed candidate is
    preferred on ties so two same-title books resolve toward the right author.
    """
    best: Optional[dict] = None
    best_key = (-1.0, 0)
    for c in candidates:
        s = book_title_score(query, c.get("title", ""), c.get("authors"),
                             c.get("original_title", ""))
        a = 1 if author_present(query, c.get("authors")) else 0
        key = (s, a)
        if key > best_key:
            best, best_key = c, key
    return best, best_key[0]


def author_overlap(hint_authors: Optional[list], meta_authors: Optional[list]) -> float:
    """Fraction of the hinted author tokens that appear in the result's authors
    (0..1). Token-based, so order and 'Last, First' vs 'First Last' don't matter."""
    def toks(lst):
        out = set()
        for a in lst or []:
            for t in _SPLIT.split((a or "").lower()):
                if len(t) >= 2:
                    out.add(t)
        return out
    h, m = toks(hint_authors), toks(meta_authors)
    if not h or not m:
        return 0.0
    return len(h & m) / len(h)


def audiobook_score(query_title: str, meta, hints: Optional[dict] = None):
    """Composite 0..100 confidence for an audiobook candidate.

    The (author-stripped) TITLE is the primary signal; authors, series, band and
    language add or subtract confidence. Returns (score, title_score, author_ov)
    so the caller can apply a strict accept gate (title strong AND author
    confirmed) and never store a wrong ASIN.
    """
    hints = hints or {}
    h_authors = hints.get("authors") or []
    m_authors = getattr(meta, "authors", None) or []
    title = book_title_score(query_title, getattr(meta, "title", ""),
                             h_authors or m_authors, getattr(meta, "original_title", ""))
    a_ov = author_overlap(h_authors, m_authors)

    bonus = 0.0
    hv, mv = hints.get("volume"), getattr(meta, "volume", None)
    if hv and mv:
        bonus += 5 if hv == mv else -10  # explicit different band => likely wrong book
    hs = (hints.get("series") or "").strip()
    ms = (getattr(meta, "series", "") or "").strip()
    if hs and ms and title_similarity(hs, ms) >= 80:
        bonus += 5
    hn = (hints.get("narrator") or "").strip()
    mn = (getattr(meta, "narrator", "") or "").strip()
    if hn and mn and title_similarity(hn, mn) >= 80:
        bonus += 5
    hl = (hints.get("language") or "")[:2].lower()
    ml = (getattr(meta, "language", "") or "")[:2].lower()
    if hl and ml:
        bonus += 2 if hl == ml else -3
    if h_authors:
        bonus += 6 * a_ov  # author confirmation strengthens the match
    score = max(0.0, min(100.0, title + bonus))
    return score, title, a_ov
