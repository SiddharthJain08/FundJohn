"""Phase 2 follow-up #17 — paper-fingerprint dedup.

Catches the same paper appearing across multiple sources (e.g., an SSRN
preprint also linked from a blog aggregator) where ON CONFLICT (source_url)
doesn't fire because the URLs differ.

Different shape from 2C (``headline_dedup.py``, headline-Jaccard for short
news headlines): this is paper-level deterministic fingerprinting.

Fingerprint shape: SHA-1(normalize(title) || first_author_lastname || year),
truncated to 16 hex chars.  Same paper across sources -> same fingerprint.
Returns ``None`` when any of (title, first author, year) is missing — partial
papers collide too easily and shouldn't be deduped.
"""
from __future__ import annotations

import hashlib
import re

_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip leading article, collapse non-alphanumeric to space,
    collapse runs of whitespace, strip ends."""
    if not title:
        return ""
    s = title.strip()
    s = _LEADING_ARTICLE_RE.sub("", s).lower()
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def extract_first_author_lastname(authors) -> str:
    """Pull the first author's last name from a list of author strings.

    Handles both ``"Last, First"`` (comma-form) and ``"First Middle Last"``
    (whitespace-form).  Returns ``""`` when input is empty / None / unusable
    so callers can short-circuit fingerprinting.
    """
    if not authors:
        return ""
    first = authors[0] or ""
    if "," in first:
        # 'Last, First' form — last name is before the comma.
        last = first.split(",", 1)[0].strip()
    else:
        # 'First Middle Last' form — last token.
        parts = first.strip().split()
        last = parts[-1] if parts else ""
    last = re.sub(r"[^a-z0-9]", "", last.lower())
    return last


def compute_fingerprint(title: str, authors, year):
    """Return a 16-hex-char SHA-1 fingerprint, or ``None`` when any input
    is missing.

    Year may be passed as int or str; ``None`` short-circuits to ``None``.
    """
    norm_title = normalize_title(title)
    last = extract_first_author_lastname(authors)
    if not norm_title or not last or year is None:
        return None
    raw = f"{norm_title}||{last}||{year}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
