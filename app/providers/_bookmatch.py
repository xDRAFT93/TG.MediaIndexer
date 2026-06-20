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
