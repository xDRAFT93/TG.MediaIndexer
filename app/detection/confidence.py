"""Confidence scoring via fuzzy string similarity.

Uses rapidfuzz when available; falls back to difflib so the system still runs
without the optional dependency.
"""
from __future__ import annotations

from ..util import normalize_title

try:
    from rapidfuzz import fuzz

    def _ratio(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b))
except Exception:  # pragma: no cover - fallback path
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0


def title_similarity(a: str, b: str) -> float:
    """0..100 similarity between two titles after normalisation."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return _ratio(na, nb)


def best_match(query: str, candidates: list[str]) -> tuple[int, float]:
    """Return (index, score) of the best matching candidate, or (-1, 0)."""
    best_idx, best_score = -1, 0.0
    for i, cand in enumerate(candidates):
        score = title_similarity(query, cand)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx, best_score
