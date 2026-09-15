"""C1 flatten action: benchmark positions are never closed, the lookup fails
CLOSED, failed submits leave pending_flatten set for the next tick, and every
attempt is journalled to circuit_breaker_fires (shadow rows carry dry_run=true
so the sizer's risk-exit cooldown ignores them).

_close_symbol is always patched — no test may reach the alpaca CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402


class FakeCursor:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        # Pop ONE batch per call (queued per execute()), not the whole queue —
        # bench_tickers() issues two sequential execute()+fetchall() pairs
        # (registry, then execution_signals) and each must see only its own
        # result set.
        return list(self._rows.pop(0)) if self._rows else []

    def fires(self):
        return [p for s, p in self.calls if 'INSERT INTO circuit_breaker_fires' in s]


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        conn_cur = self._cur

        class _Ctx:
            def __enter__(self_inner):
                return conn_cur

            def __exit__(self_inner, *_a):
                return False

        return _Ctx()


POSITIONS = {
    'SPY':  {'qty': 200.0,  'side': 'long',  'market_value': '41000'},
    'AAPL': {'qty': 100.0,  'side': 'long',  'market_value': '22000'},
    'AMD':  {'qty': -50.0,  'side': 'short', 'market_value': '-7000'},
    'FLAT': {'qty': 0.0,    'side': 'long',  'market_value': '0'},
}
ST = {'peak': 171_200.0, 'dd': -0.1291, 'daily': -0.0727,
      'rule': 'drawdown', 'breach': True}


# ── benchmark ticker lookup ─────────────────────────────────────────────────

def test_bench_tickers_reads_registry_then_recent_signals():
    cur = FakeCursor([[('S_beta_spy',)], [('SPY',)]])
    conn = FakeConn(cur)
    assert ab.bench_tickers(conn) == {'SPY'}
    assert any('strategy_registry' in s for s, _ in cur.calls)
    assert any('execution_signals' in s for s, _ in cur.calls)


def test_bench_tickers_no_sleeve_is_an_empty_set_not_none():
    cur = FakeCursor([[]])
    assert ab.bench_tickers(FakeConn(cur)) == set()


def test_bench_tickers_fails_closed_to_none_on_error():
    class Boom:
        def cursor(self):
            raise RuntimeError('db down')

    assert ab.bench_tickers(Boom()) is None


# ── flatten ─────────────────────────────────────────────────────────────────

def test_flatten_skips_benchmark_and_zero_qty_positions(monkeypatch):
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert closed == ['AAPL', 'AMD']          # sorted, SPY and FLAT excluded
    assert out == {'ok': 2, 'fail': 0, 'pending': False, 'tickers': ['AAPL', 'AMD']}


def test_flatten_benchmark_exemption_is_case_insensitive(monkeypatch):
    """Mirrors alpha_nav's I-1 fix (commit 4dcf6693): bench_tkrs comes from
    execution_signals.ticker while positions keys come from the broker, and a
    casing mismatch must never close the benchmark sleeve."""
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    ab.flatten_alpha(POSITIONS, {'spy'}, cur=FakeCursor(), live=True,
                     rule='drawdown', magnitude=ST['dd'])
    assert 'SPY' not in closed
    assert closed == ['AAPL', 'AMD']


def test_second_call_after_full_flatten_submits_nothing(monkeypatch):
    """Idempotency (binding safety constraint): once a position's broker qty
    reads back as 0 (post-flatten), a repeat call must submit nothing."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError('nothing left to close')))
    flat_positions = {'AAPL': {'qty': 0.0, 'market_value': '0'},
                      'SPY': {'qty': 200.0, 'market_value': '41000'}}
    out = ab.flatten_alpha(flat_positions, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 0, 'fail': 0, 'pending': False, 'tickers': []}


def test_flatten_skips_option_and_crypto_symbols(monkeypatch):
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    positions = {
        'AAPL260918C00250000': {'qty': 2.0, 'market_value': '400'},
        'BTC/USD': {'qty': 0.5, 'market_value': '30000'},
        'MSFT': {'qty': 10.0, 'market_value': '4000'},
    }
    ab.flatten_alpha(positions, set(), cur=FakeCursor(), live=True,
                     rule='drawdown', magnitude=-0.13)
    assert closed == ['MSFT']


def test_failed_submit_counts_and_sets_pending(monkeypatch):
    def _close(sym, qty, market_open=None):
        if sym == 'AMD':
            return False, {'error': 'insufficient qty'}
        return True, {}

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['fail'] == 1 and out['pending'] is True


def test_close_symbol_raising_is_a_failure_not_a_crash(monkeypatch):
    def _close(sym, qty, market_open=None):
        raise RuntimeError('cli exploded')

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha({'AAPL': {'qty': 1.0, 'market_value': '100'}}, set(),
                           cur=FakeCursor(), live=True, rule='daily_loss',
                           magnitude=-0.05)
    assert out['fail'] == 1 and out['pending'] is True


def test_shadow_mode_submits_nothing_and_journals_dry_run(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError('shadow mode must not submit an order')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=False,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 2, 'fail': 0, 'pending': False, 'tickers': ['AAPL', 'AMD']}
    payloads = [json.loads(p[5]) for p in cur.fires()]
    assert payloads and all(p['dry_run'] is True for p in payloads)
    assert all(p['account_breaker'] is True and p['rule'] == 'drawdown'
               for p in payloads)


def test_live_fire_rows_carry_the_rule_threshold_and_signed_qty(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    cur = FakeCursor()
    ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                     rule='drawdown', magnitude=ST['dd'])
    by_ticker = {p[1]: p for p in cur.fires()}
    assert set(by_ticker) == {'AAPL', 'AMD'}
    assert by_ticker['AMD'][4] == -50.0                  # signed position_qty
    assert by_ticker['AAPL'][3] == pytest.approx(0.10)   # threshold_pct = |DD_LIMIT|
    assert by_ticker['AAPL'][2] == pytest.approx(-0.1291)
    assert json.loads(by_ticker['AAPL'][5])['dry_run'] is False


def test_journal_false_writes_nothing(monkeypatch):
    """main() uses journal=False in SHADOW: the spec allows a log line only, and
    a sustained breach would otherwise write dry-run rows every 5 minutes."""
    def _boom(*_a, **_k):
        raise AssertionError('shadow mode must not submit an order')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=False,
                           rule='drawdown', magnitude=ST['dd'], journal=False)
    assert out['ok'] == 2 and cur.fires() == []


def test_rule_threshold_and_magnitude_select_the_breaching_rule():
    assert ab.rule_threshold('drawdown') == pytest.approx(0.10)
    assert ab.rule_threshold('daily_loss') == pytest.approx(0.03)
    assert ab.rule_threshold('drawdown+daily_loss') == pytest.approx(0.10)
    assert ab.rule_magnitude('daily_loss', ST) == pytest.approx(-0.0727)
    assert ab.rule_magnitude('drawdown', ST) == pytest.approx(-0.1291)
