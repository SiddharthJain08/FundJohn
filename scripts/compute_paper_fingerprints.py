#!/usr/bin/env python3
"""Batch fingerprint CLI for the JS ingestor (paper_expansion_ingestor.js).

Reads a JSON array of paper records from stdin:
  [{"title": "...", "authors": ["..."], "year": 2024}, ...]
Writes a JSON array of fingerprints (or null when unfingerprintable) to stdout:
  ["3a1c...d4", null, "9b8e...02", ...]

Single subprocess for the whole batch — keeps per-paper overhead bounded.

Usage from JS:
  const { execFileSync } = require('node:child_process');
  const out = execFileSync('python3',
    ['scripts/compute_paper_fingerprints.py'],
    { input: JSON.stringify(papers), encoding: 'utf-8' });
  const fps = JSON.parse(out);
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.research.paper_fingerprint import compute_fingerprint


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("[]")
        return 0
    papers = json.loads(raw)
    out = []
    for p in papers:
        fp = compute_fingerprint(
            p.get("title") or "",
            p.get("authors") or [],
            p.get("year"),
        )
        out.append(fp)
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
