"""C1 flatten action: benchmark positions are never closed, the lookup fails
CLOSED, failed/partial/stuck closes leave pending_flatten set for the next
tick, and every real attempt is journalled to circuit_breaker_fires (shadow
rows carry dry_run=true so the sizer's risk-exit cooldown ignores them).

_close_symbol, _load_open_orders and _load_broker_positions are ALWAYS
patched (see the autouse fixture below) — no test may reach the alpaca CLI.

Fix round 1 (2026-09-12 qd-stream-c-risk, task 5): flatten_alpha's return
shape gained 'partial' and 'aborted'; a post-loop broker re-read now catches
submit-ok-but-still-held positions; a pre-loop open-orders check skips
resubmitting a symbol that already has a working close order; bench_tickers
now takes `cur` (not `conn`) and fails CLOSED when a benchmark-sleeve
strategy resolves to zero tickers.

Fix round 2: an EMPTY post-loop re-read ({} — _load_broker_positions'
primary failure mode) is now treated as UNKNOWN (still open), never as "all
flat"; the settle delay is a module constant `_SETTLE_S` tests zero out
directly instead of patching `time.sleep`.
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


POSITIONS = {
    'SPY':  {'qty': 200.0,  'side': 'long',  'market_value': '41000'},
    'AAPL': {'qty': 100.0,  'side': 'long',  'market_value': '22000'},
    'AMD':  {'qty': -50.0,  'side': 'short', 'market_value': '-7000'},
    'FLAT': {'qty': 0.0,    'side': 'long',  'market_value': '0'},
}
ST = {'peak': 171_200.0, 'dd': -0.1291, 'daily': -0.0727,
      'rule': 'drawdown', 'breach': True}


@pytest.fixture(autouse=True)
def _no_real_broker_calls(monkeypatch):
    """Default double for every new broker touchpoint flatten_alpha added in
    fix round 1/2: no working close orders resting, and the post-loop
    re-read returns a realistic non-empty book (just the always-held SPY
    benchmark leg) that never contains a touched symbol — i.e. every
    submitted close reads back as genuinely flat, not "unknown". This must
    be non-empty: fix round 2, item 1 makes an EMPTY {} re-read mean
    "unknown, treat every attempted symbol as still open", so a `{}` default
    here would flip most of this file's happy-path assertions to
    pending=True. Individual tests override via monkeypatch for their own
    scenario. Also zeroes the settle delay via the module constant (fix
    round 2, nit 1 — `_SETTLE_S`, not `time.sleep` itself) so the suite
    doesn't pay it 20+ times. No test may reach the real alpaca CLI."""
    monkeypatch.setattr(ab, '_SETTLE_S', 0)
    monkeypatch.setattr(rl, '_load_open_orders', lambda: [])
    monkeypatch.setattr(rl, '_load_broker_positions',
                        lambda: {'SPY': {'qty': 200.0, 'side': 'long',
                                         'market_value': '41000'}})


# ── benchmark ticker lookup ─────────────────────────────────────────────────

def test_bench_tickers_reads_registry_then_recent_signals():
    cur = FakeCursor([[('S_beta_spy',)], [('SPY',)]])
    assert ab.bench_tickers(cur) == {'SPY'}
    assert any('strategy_registry' in s for s, _ in cur.calls)
    assert any('execution_signals' in s for s, _ in cur.calls)


def test_bench_tickers_no_sleeve_is_an_empty_set_not_none():
    cur = FakeCursor([[]])
    assert ab.bench_tickers(cur) == set()


def test_bench_tickers_ids_present_but_zero_tickers_resolved_is_none():
    """Fix round 1, item 2: a benchmark-sleeve strategy id EXISTS but
    execution_signals has nothing recent for it — that is NOT the same as
    "no sleeve configured" and must fail CLOSED, not degrade to set()."""
    cur = FakeCursor([[('S_beta_spy',)], []])
    assert ab.bench_tickers(cur) is None


def test_bench_tickers_fails_closed_to_none_on_error():
    class BoomCursor:
        def execute(self, *_a, **_k):
            raise RuntimeError('db down')

    assert ab.bench_tickers(BoomCursor()) is None


# ── flatten: benchmark/zero-qty/option/crypto exemptions ────────────────────

def test_flatten_skips_benchmark_and_zero_qty_positions(monkeypatch):
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert closed == ['AAPL', 'AMD']          # sorted, SPY and FLAT excluded
    assert out == {'ok': 2, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': ['AAPL', 'AMD']}


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


# ── abort / nothing-to-close ─────────────────────────────────────────────────

def test_flatten_aborts_when_bench_tickers_is_none(monkeypatch):
    """Fail-closed, second gate: bench_tickers(cur) returning None means the
    lookup failed. flatten_alpha must ABORT — submit and journal nothing —
    rather than silently treating None as 'no benchmarks'. Fix round 1, item
    4: the abort outcome is now distinguishable from nothing-to-close:
    pending=True and aborted=True (not the old pending=False)."""
    def _boom(*_a, **_k):
        raise AssertionError('must not submit when the bench lookup failed')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, None, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 0, 'fail': 0, 'partial': 0, 'pending': True,
                   'aborted': True, 'tickers': []}
    assert cur.fires() == []


def test_second_call_after_full_flatten_submits_nothing(monkeypatch):
    """Idempotency (binding safety constraint): once a position's broker qty
    reads back as 0 (post-flatten), a repeat call must submit nothing — this
    is also the 'nothing to close' outcome, distinct from 'aborted'."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError('nothing left to close')))
    flat_positions = {'AAPL': {'qty': 0.0, 'market_value': '0'},
                      'SPY': {'qty': 200.0, 'market_value': '41000'}}
    out = ab.flatten_alpha(flat_positions, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 0, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': []}


def test_nothing_to_close_vs_aborted_are_distinguishable():
    nothing = ab.flatten_alpha({'SPY': {'qty': 200.0, 'market_value': '41000'}},
                               {'SPY'}, cur=FakeCursor(), live=True,
                               rule='drawdown', magnitude=ST['dd'])
    aborted = ab.flatten_alpha(POSITIONS, None, cur=FakeCursor(), live=True,
                               rule='drawdown', magnitude=ST['dd'])
    assert nothing == {'ok': 0, 'fail': 0, 'partial': 0, 'pending': False,
                       'aborted': False, 'tickers': []}
    assert aborted == {'ok': 0, 'fail': 0, 'partial': 0, 'pending': True,
                       'aborted': True, 'tickers': []}
    assert nothing != aborted


# ── failures, exceptions ─────────────────────────────────────────────────────

def test_failed_submit_counts_and_sets_pending(monkeypatch):
    def _close(sym, qty, market_open=None):
        if sym == 'AMD':
            return False, {'error': 'insufficient qty'}
        return True, {}

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['fail'] == 1 and out['partial'] == 0
    assert out['pending'] is True


def test_close_symbol_raising_is_a_failure_not_a_crash(monkeypatch):
    def _close(sym, qty, market_open=None):
        raise RuntimeError('cli exploded')

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha({'AAPL': {'qty': 1.0, 'market_value': '100'}}, set(),
                           cur=FakeCursor(), live=True, rule='daily_loss',
                           magnitude=-0.05)
    assert out['fail'] == 1 and out['partial'] == 0 and out['pending'] is True


# ── partial: payload flag, working close order, post-loop re-read ───────────

def test_partial_flatten_payload_counts_as_partial_and_pending(monkeypatch):
    """_close_symbol's own partial-fill payload (cancel-then-close hostage
    residual) is counted as partial immediately, not ok."""
    def _close(sym, qty, market_open=None):
        if sym == 'AMD':
            return True, {'partial_flatten': True, 'closed_qty': 30, 'hostage_qty': 20}
        return True, {'status': 'filled'}

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['fail'] == 0
    assert out['partial'] == 1 and out['pending'] is True


def test_submit_ok_but_still_held_on_reread_is_partial(monkeypatch):
    """_close_symbol ok=True means SUBMITTED, not filled: if the post-loop
    re-read still shows the position, the symbol must move ok -> partial."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'accepted'}))
    monkeypatch.setattr(rl, '_load_broker_positions',
                        lambda: {'AMD': {'qty': -50.0, 'market_value': '-7000'}})
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1                      # AAPL: gone from the re-read
    assert out['partial'] == 1 and out['fail'] == 0   # AMD: still held
    assert out['pending'] is True


def test_reread_treats_unparseable_qty_as_still_open(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'accepted'}))
    monkeypatch.setattr(rl, '_load_broker_positions',
                        lambda: {'AMD': {'qty': 'not-a-number', 'market_value': '-7000'}})
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['partial'] == 1 and out['pending'] is True


def test_reread_empty_mapping_is_unknown_not_flat(monkeypatch, caplog):
    """Fix round 2, item 1 (the Important item): `_load_broker_positions()`
    returns {} (no exception) on a non-zero CLI exit or a non-list payload —
    its PRIMARY failure mode. An empty mapping must be treated as UNKNOWN
    (every attempted symbol still counts as open), never as "the book is
    empty so everything is flat" — the latter would silently clear
    pending_flatten on a halted breaker that never re-evaluates, so an
    unfilled close would never be retried. See the paired test below for the
    genuinely-flat case this must stay distinguishable from."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: {})
    with caplog.at_level('WARNING'):
        out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                               rule='drawdown', magnitude=ST['dd'])
    assert out['pending'] is True
    assert out['partial'] == len(out['tickers']) == 2
    assert any('flatten re-read unavailable' in r.message for r in caplog.records)


def test_reread_non_empty_book_without_touched_symbols_is_flat(monkeypatch):
    """The distinguishing case for the fix above: a non-empty re-read that
    simply no longer contains the touched symbols means the book WAS
    successfully read and the close really is done — pending=False, unlike
    the {} case above, which must stay pending=True."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    monkeypatch.setattr(rl, '_load_broker_positions',
                        lambda: {'SPY': {'qty': 200.0, 'market_value': '41000'}})
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['pending'] is False
    assert out['partial'] == 0


def test_reread_failure_treats_every_attempted_symbol_as_still_open(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))

    def _boom():
        raise RuntimeError('cli timeout')

    monkeypatch.setattr(rl, '_load_broker_positions', _boom)
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 0 and out['partial'] == 2 and out['pending'] is True


def test_fail_and_partial_can_both_count_the_same_symbol(monkeypatch):
    """A submit failure (fail bucket) whose position genuinely still shows
    non-zero on the re-read ALSO increments partial — fail answers 'did the
    submit work', partial answers 'is it flat'. Nothing sums the two against
    len(tickers) (Task 7 reads only `pending`), so this double-count is
    deliberate, not a bug — see flatten_alpha's docstring."""
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (False, {'error': 'insufficient qty'}))
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: dict(POSITIONS))
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['fail'] == 2 and out['partial'] == 2 and out['ok'] == 0
    assert out['pending'] is True


def test_working_close_order_is_not_resubmitted(monkeypatch):
    """Retry safety: a symbol with a WORKING market close already resting on
    the close side must not be resubmitted or cancelled — just counted
    partial, with no fire row for the no-op attempt. type='market' is what
    distinguishes this from an ordinary protective bracket leg (see the
    paired negative test below) — _close_symbol's own close is always a
    market order."""
    def _boom(sym, qty, market_open=None):
        if sym == 'AMD':
            raise AssertionError('must not resubmit a working close order')
        return True, {'status': 'filled'}

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    monkeypatch.setattr(rl, '_load_open_orders',
                        lambda: [{'symbol': 'AMD', 'side': 'buy', 'type': 'market',
                                  'status': 'open'}])
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['partial'] == 1 and out['pending'] is True
    fired_tickers = {p[1] for p in cur.fires()}
    assert 'AMD' not in fired_tickers           # no-op attempt: no fire row
    assert 'AAPL' in fired_tickers


def test_working_close_order_symbol_match_is_case_insensitive(monkeypatch):
    def _boom(sym, qty, market_open=None):
        if sym == 'AMD':
            raise AssertionError('must not resubmit a working close order')
        return True, {'status': 'filled'}

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    monkeypatch.setattr(rl, '_load_open_orders',
                        lambda: [{'symbol': 'amd', 'side': 'buy', 'type': 'market',
                                  'status': 'open'}])
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['partial'] == 1


def test_working_order_on_the_wrong_side_does_not_block_resubmit(monkeypatch):
    """An open order on a symbol that is NOT on the close side (e.g. a stray
    buy resting on a position we are also buying to close, i.e. AMD short ->
    close side is buy — so give AAPL, a long, a resting BUY, which is not its
    sell-to-close side) must not suppress the resubmit."""
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {'status': 'filled'}))[1])
    monkeypatch.setattr(rl, '_load_open_orders',
                        lambda: [{'symbol': 'AAPL', 'side': 'buy', 'type': 'market',
                                  'status': 'open'}])
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert 'AAPL' in closed
    assert out['ok'] == 2 and out['partial'] == 0


def test_protective_bracket_legs_do_not_block_resubmit(monkeypatch):
    """The blocker an advisor review caught: a bracketed long's resting
    take-profit (SELL LIMIT) and stop-loss (SELL STOP) are both on the
    position's own close side — side alone would flag them as "already
    closing" and the breaker would never actually flatten a bracketed
    position (which is the normal state of the whole OpenClaw book, since
    every entry is bracketed). type=='market' is what excludes them."""
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {'status': 'filled'}))[1])
    monkeypatch.setattr(rl, '_load_open_orders',
                        lambda: [
                            {'symbol': 'AAPL', 'side': 'sell', 'type': 'limit',
                             'status': 'open', 'client_order_id': 'AX123'},
                            {'symbol': 'AAPL', 'side': 'sell', 'type': 'stop',
                             'status': 'open', 'client_order_id': 'AX124'},
                            {'symbol': 'AMD', 'side': 'buy', 'type': 'stop_limit',
                             'status': 'open', 'client_order_id': 'AX125'},
                        ])
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert closed == ['AAPL', 'AMD']            # both resubmitted, not skipped
    assert out == {'ok': 2, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': ['AAPL', 'AMD']}


def test_full_flatten_is_pending_false(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 2, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': ['AAPL', 'AMD']}


# ── _record_fire resilience ──────────────────────────────────────────────────

class BoomOnFireInsertCursor(FakeCursor):
    """Raises only on the circuit_breaker_fires INSERT — SAVEPOINT/ROLLBACK/
    RELEASE calls from _savepoint_guarded succeed normally, so this exercises
    exactly the guarded write failing without poisoning the whole cursor."""

    def execute(self, sql, params=None):
        if 'INSERT INTO circuit_breaker_fires' in sql:
            raise RuntimeError('db down')
        super().execute(sql, params)


def test_record_fire_raising_does_not_stop_remaining_closes(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    cur = BoomOnFireInsertCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 2, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': ['AAPL', 'AMD']}
    assert cur.fires() == []                    # every INSERT failed + was swallowed


# ── shadow mode ───────────────────────────────────────────────────────────────

def test_shadow_mode_submits_nothing_and_journals_dry_run(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError('shadow mode must not submit an order')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    monkeypatch.setattr(rl, '_load_open_orders', _boom)
    monkeypatch.setattr(rl, '_load_broker_positions', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=False,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 2, 'fail': 0, 'partial': 0, 'pending': False,
                   'aborted': False, 'tickers': ['AAPL', 'AMD']}
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
