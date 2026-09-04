"""Benchmark sleeve tickers are exempt from the premarket news veto (2026-09-04).

The buy-and-hold beta sleeve (S_beta_spy -> SPY) exists to hold the benchmark
through ALL news. A market ETF inherits every macro headline, so it ran panic
72-84 every morning and survived only on the LLM confirmer's mercy — until
2026-09-04, when one bearish call ejected the whole 104-share sleeve at the
open and entry_hygiene blocked the $80k beta budget all day. The gate still
SCORES benchmark tickers (the audit row keeps the real panic number) but can
never REJECT them; a failed sleeve lookup falls back to judging them like any
other holding (fail-open toward the old behavior).

Pure unit tests — fake cursors, injected broker loaders. No DB, no broker.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import benchmark_sleeve as bs  # noqa: E402
from execution import premarket_gate as pg  # noqa: E402


@pytest.fixture
def sameday(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', '1')
    monkeypatch.delenv('OPENCLAW_SAMEDAY_PREMARKET_PROTECT', raising=False)
    monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')


class _Cur:
    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def wrote(self, needle):
        return [p for sql, p in self.statements if needle in ' '.join(sql.split())]


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass


def _run(monkeypatch, *, book, bench, panic=90.0):
    monkeypatch.setattr(pg, '_post_premarket_alert', lambda v, d: None)
    monkeypatch.setattr(pg, 'score_news_for_tickers',
                        lambda tickers, since: [
                            {'ticker': t, 'news_count_24h': 8, 'news_finbert_neg': 4,
                             'news_mean_score': -0.8, 'news_top_headlines': ['h'],
                             'evidence_uuids': ['u']} for t in tickers])
    monkeypatch.setattr(pg, 'panic_score', lambda inp: panic)
    monkeypatch.setattr(pg, 'confirm_panic', lambda inp: type(
        'R', (), {'verdict': 'bearish_news_driven', 'severity': 4, 'rationale': ''})())
    monkeypatch.setattr(pg, '_load_carried_signals', lambda cur, d: [])
    monkeypatch.setattr(pg, 'benchmark_sleeve_universe_tickers', bench)
    cur = _Cur()
    out = pg.run_gate(conn=_Conn(cur), broker_loader=lambda: book)
    return out, cur


def _hold_verdicts(cur):
    # _write_gate_verdict param order: (signal_id, gate_type, ticker,
    # target_date, verdict, panic, news_count, severity, model, metadata, ...)
    return {p[2]: p for p in cur.wrote('INSERT INTO signal_gate_verdicts')
            if p[1] == pg.GATE_TYPE_HOLD}


def test_benchmark_ticker_survives_panic_but_alpha_is_vetoed(sameday, monkeypatch):
    out, cur = _run(monkeypatch, book={'SPY': 64000.0, 'AAPL': 5000.0},
                    bench=lambda conn=None: {'SPY'})
    rows = _hold_verdicts(cur)
    assert rows['SPY'][4] == 'APPROVED'
    assert rows['SPY'][8] == 'benchmark_sleeve_exempt'
    assert '"benchmark_exempt": true' in rows['SPY'][9]
    assert rows['AAPL'][4] == 'REJECTED'
    assert out['n_rejected'] == 1 and out['n_approved'] == 1


def test_exempt_row_keeps_the_real_panic_score(sameday, monkeypatch):
    _, cur = _run(monkeypatch, book={'SPY': 64000.0},
                  bench=lambda conn=None: {'SPY'}, panic=84.0)
    assert float(_hold_verdicts(cur)['SPY'][5]) == 84.0


def test_calm_benchmark_stays_plain_rule_approved(sameday, monkeypatch):
    """Below threshold the exempt branch never engages — the audit row must
    not claim an exemption did the approving."""
    _, cur = _run(monkeypatch, book={'SPY': 64000.0},
                  bench=lambda conn=None: {'SPY'}, panic=1.0)
    row = _hold_verdicts(cur)['SPY']
    assert row[4] == 'APPROVED' and row[8] == 'rule_based_panic_score'
    assert '"benchmark_exempt": true' in row[9]  # membership still audited


def test_lookup_failure_falls_back_to_judging(sameday, monkeypatch):
    def boom(conn=None):
        raise RuntimeError('registry down')
    out, cur = _run(monkeypatch, book={'SPY': 64000.0}, bench=boom)
    assert _hold_verdicts(cur)['SPY'][4] == 'REJECTED'
    assert out['gate_ran'] is True  # the failure never takes the gate down


def test_no_sleeves_means_no_change(sameday, monkeypatch):
    _, cur = _run(monkeypatch, book={'SPY': 64000.0}, bench=lambda conn=None: set())
    assert _hold_verdicts(cur)['SPY'][4] == 'REJECTED'


# --- the sleeve->ticker helper itself ----------------------------------------

class _RegistryCur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RegistryConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _RegistryCur(self._rows)


def test_helper_extracts_literal_tickers_only():
    conn = _RegistryConn([(['SPY'],), (['FixedETFlist:VNQ,XLK', 'BRK.B'],), (None,)])
    assert bs.benchmark_sleeve_universe_tickers(conn) == {'SPY', 'BRK.B'}


def test_helper_fails_open_empty():
    class _Boom:
        def cursor(self):
            raise RuntimeError('db down')
    assert bs.benchmark_sleeve_universe_tickers(_Boom()) == set()
