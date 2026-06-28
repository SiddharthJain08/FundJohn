"""Tests for edgar._write_cik_cache best-effort cache write.

Added in the W1 reconcile pass. The SEC ticker->CIK cache
(data/master/_sec_ticker_cik.json) is root-owned; a write failure (e.g. a
permission regression) must NOT abort the whole 8-K ingest, since the CIK dict
is already in memory. Hermetic: no network / SEC calls.

Run:
    pytest tests/test_edgar_cik_cache.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from pipeline.backfillers import edgar  # noqa: E402


def test_write_cik_cache_success(tmp_path):
    p = tmp_path / '_sec_ticker_cik.json'
    ok = edgar._write_cik_cache(p, {'AAPL': '0000320193'})
    assert ok is True
    assert json.loads(p.read_text())['AAPL'] == '0000320193'


def test_write_cik_cache_permission_error_is_nonfatal():
    cache = MagicMock()
    cache.write_text.side_effect = PermissionError(13, 'Permission denied')
    # Must NOT raise; returns False so the caller proceeds with in-memory data.
    assert edgar._write_cik_cache(cache, {'AAPL': '0000320193'}) is False
