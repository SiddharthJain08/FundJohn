"""scripts/ingest_edgar_8k.py — per-run ticker cap.

The premarket 8-K ingester is fed the full open book (~270 equity positions
in 2026-08) but capped at OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN, which
defaulted to 50 — so every run silently dropped ~80% of the book after
'truncating 271 tickers to max 50' (journal 08-21). The EDGAR client
self-throttles to 10 req/s, so a full book is ~30-60s; the default must fit
the whole book and the truncation must say how many names were dropped.
"""
from __future__ import annotations

import logging

import pytest

from scripts import ingest_edgar_8k as mod


def test_default_cap_fits_full_book(monkeypatch):
    monkeypatch.delenv('OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN', raising=False)
    assert mod._max_tickers_per_run() >= 400


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN', '7')
    assert mod._max_tickers_per_run() == 7


def test_no_truncation_under_cap(caplog):
    tickers = [f'T{i}' for i in range(10)]
    with caplog.at_level(logging.WARNING, logger=mod.log.name):
        out = mod._cap_tickers(tickers, 50)
    assert out == tickers
    assert not [r for r in caplog.records if 'truncat' in r.getMessage()]


def test_truncation_warns_with_dropped_count(caplog):
    tickers = [f'T{i}' for i in range(450)]
    with caplog.at_level(logging.WARNING, logger=mod.log.name):
        out = mod._cap_tickers(tickers, 400)
    assert out == tickers[:400]
    msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1
    text = msgs[0].getMessage()
    assert 'dropped 50' in text and '450' in text and '400' in text
    assert 'OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN' in text
