"""Universe shrink core (campaign W3): point-in-time bucketing + tier metric
derivation from stored full-universe trades, and the prefer-largest selection
wired against the full-universe baseline."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd

from backtest.universe_shrink import (
    FULL_TIER, TOTAL_KEY, bucket_trades, db_rows, mean_tier_sizes,
    shrink_and_select, snap_for,
)

SNAPS = ['2024-01-31', '2024-02-29', '2024-03-31']
MEMBERS = {
    'sp500':       {s: frozenset({'AAA'}) for s in SNAPS},
    'tier_r1000':  {s: frozenset({'AAA', 'BBB'}) for s in SNAPS},
    'tier_r3000':  {s: frozenset({'AAA', 'BBB', 'CCC'}) for s in SNAPS},
    'tier_liquid': {s: frozenset({'AAA', 'BBB', 'CCC', 'DDD'}) for s in SNAPS},
}


def _trade(ticker, entry, pnl=0.01, hold=3, regime='LOW_VOL'):
    return {'ticker': ticker, 'entry_date': date.fromisoformat(entry),
            'pnl_pct': pnl, 'holding_days': hold, 'entry_regime': regime}


def test_snap_for_prior_month_end():
    assert snap_for('2024-02-15', SNAPS) == '2024-01-31'
    assert snap_for('2024-01-31', SNAPS) == '2024-01-31'   # on the snap itself
    assert snap_for('2024-01-01', SNAPS) is None           # before first snap


def test_bucketing_respects_nesting():
    trades = [_trade('AAA', '2024-02-10'), _trade('CCC', '2024-02-10'),
              _trade('ZZZ', '2024-02-10'),          # in no tier (full only)
              _trade('DDD', '2024-01-05')]          # before first snap → dropped
    b = bucket_trades(trades, SNAPS, MEMBERS)
    assert [t['ticker'] for t in b['sp500']] == ['AAA']
    assert [t['ticker'] for t in b['tier_r3000']] == ['AAA', 'CCC']
    assert [t['ticker'] for t in b['tier_liquid']] == ['AAA', 'CCC']
    # nesting: everything a narrow tier kept, every broader tier kept too
    sp = {id(t) for t in b['sp500']}
    assert sp <= {id(t) for t in b['tier_liquid']}


def test_membership_is_point_in_time():
    members = {t: dict(m) for t, m in MEMBERS.items()}
    members['sp500'] = {'2024-01-31': frozenset({'AAA'}),
                        '2024-02-29': frozenset(),        # AAA dropped in Feb
                        '2024-03-31': frozenset()}
    early = _trade('AAA', '2024-02-10')   # resolves via Jan snap → member
    late = _trade('AAA', '2024-03-10')    # resolves via Feb snap → not member
    b = bucket_trades([early, late], SNAPS, members)
    assert [t['entry_date'] for t in b['sp500']] == [early['entry_date']]


def _regimes_series():
    idx = pd.bdate_range('2024-01-01', '2024-04-30')
    return pd.Series('LOW_VOL', index=idx)


DAY_FREQ = {'LOW_VOL': 1.0, 'TRANSITIONING': 0.0, 'HIGH_VOL': 0.0, 'CRISIS': 0.0}


def test_shrink_and_select_prefers_largest_and_persists_full_baseline():
    # 40 profitable AAA trades (in every tier) + 40 losing DDD trades that
    # only tier_liquid keeps → sp500 has the higher Sharpe by miles, but the
    # maintain-constraint is NOT violated (full universe never met 100
    # trades in any regime) so displacement is allowed on Sharpe alone.
    trades = ([_trade('AAA', f'2024-02-{d:02d}', pnl=0.02 + 0.002 * (d % 5))
               for d in range(1, 21)]
              + [_trade('AAA', f'2024-03-{d:02d}', pnl=0.02 + 0.003 * (d % 4))
                 for d in range(1, 21)]
              + [_trade('DDD', f'2024-02-{d:02d}', pnl=-0.03 - 0.002 * (d % 3))
                 for d in range(1, 21)]
              + [_trade('DDD', f'2024-03-{d:02d}', pnl=-0.03 - 0.004 * (d % 5))
                 for d in range(1, 21)])
    res = shrink_and_select(trades, SNAPS, MEMBERS, _regimes_series(), DAY_FREQ)
    assert res['metrics_by_tier'][FULL_TIER]['trades_n'] == 80
    assert res['metrics_by_tier']['sp500']['trades_n'] == 40
    assert res['verdict']['verdict'] == 'winner'
    # sp500 / r1000 / r3000 hold the IDENTICAL trade subset (AAA only), so
    # they tie on Sharpe: the shrink stops at the LARGEST of the tied tiers —
    # tier_r3000 displaces tier_liquid (Δ >> 0.1) but sp500/r1000 cannot beat
    # r3000 by 0.1, so prefer-largest keeps r3000.
    assert res['verdict']['choice'] == 'tier_r3000'


def test_shrink_blocked_by_full_universe_regime_qualification():
    # Full universe qualifies LOW_VOL (120 trades, small DD); sp500 keeps
    # only 40 of them → drops below the 100-trade floor → sp500 may NOT
    # displace tier_liquid even with a huge Sharpe edge.
    trades = []
    for m, last in (('2024-02', 20), ('2024-03', 20)):
        for d in range(1, last + 1):
            trades.append(_trade('AAA', f'{m}-{d:02d}',
                                 pnl=0.02 + 0.002 * (d % 5)))
            trades.append(_trade('DDD', f'{m}-{d:02d}',
                                 pnl=-0.001 - 0.0005 * (d % 3)))
            trades.append(_trade('CCC', f'{m}-{d:02d}',
                                 pnl=-0.001 - 0.0004 * (d % 4)))
    res = shrink_and_select(trades, SNAPS, MEMBERS, _regimes_series(), DAY_FREQ)
    assert res['metrics_by_tier'][FULL_TIER]['trades_n'] == 120
    v = res['verdict']
    assert v['maintained_regimes'] == ['LOW_VOL']
    assert v['choice'] == 'tier_liquid'
    blocked = [c for c in v['comparisons'] if c['challenger'] == 'sp500']
    assert blocked and blocked[0]['blocked_regimes'] == ['LOW_VOL']


def test_db_rows_shape():
    trades = [_trade('AAA', '2024-02-10')] * 30
    res = shrink_and_select(trades, SNAPS, MEMBERS, _regimes_series(), DAY_FREQ)
    rows = db_rows('S_x', 'run-1', res, candidate_set_id='shrink-1-test')
    # 5 tiers (4 ladder + full) × (TOTAL + 4 canonical regimes) = 25 rows
    assert len(rows) == 25
    tiers = {r[2] for r in rows}
    assert tiers == {'sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid', FULL_TIER}
    total = [r for r in rows if r[2] == 'sp500' and r[3] == TOTAL_KEY][0]
    assert total[0] == 'S_x' and total[6] == 30          # trade_count
    assert all(r[12] == 'shrink-1-test' for r in rows)   # candidate_set_id


def test_mean_tier_sizes_window_scoped():
    sizes = mean_tier_sizes(SNAPS, MEMBERS, '2024-01-01', '2024-02-28')
    assert sizes['sp500'] == 1.0 and sizes['tier_liquid'] == 4.0
    assert mean_tier_sizes(SNAPS, MEMBERS, '2030-01-01', '2030-12-31')['sp500'] is None


# ── marks-based shrink (2026-11-24 shrink Sharpe fix, Part B) ───────────────
def _closes(start='2024-01-02', end='2024-04-30', step=0.003):
    idx = pd.bdate_range(start, end)
    vals = [100.0 * (1.0 + step * ((i % 7) - 3)) for i in range(len(idx))]
    return pd.Series(vals, index=pd.DatetimeIndex(idx), dtype=float)


def test_prepare_for_shrink_attaches_marks():
    from backtest.universe_shrink import prepare_for_shrink
    closes = {'AAA': _closes()}
    with_path = dict(_trade('AAA', '2024-02-05', pnl=0.05, hold=3),
                     direction='LONG', exit_date=date(2024, 2, 16))
    no_path = dict(_trade('ZZZ', '2024-02-05', pnl=0.02, hold=3),
                   direction='SHORT', exit_date=date(2024, 2, 16))
    out = prepare_for_shrink([with_path, no_path], closes.get)
    assert 'daily_marks' not in with_path              # inputs not mutated
    marks = out[0]['daily_marks']
    assert marks and all(pd.Timestamp('2024-02-05') < d <= pd.Timestamp('2024-02-16')
                         for d, _ in marks)
    prod = 1.0
    for _, r in marks:
        prod *= 1.0 + r
    assert abs(prod - 1.05) < 1e-9                     # endpoint-exact
    assert 'daily_marks' not in out[1]                 # no closes → smear
    assert out[1]['holding_days'] == 11                # calendar span
    # bucketing keys pass through untouched
    assert out[0]['ticker'] == 'AAA' and out[0]['entry_date'] == date(2024, 2, 5)


def test_shrink_and_select_aggregates_on_marks():
    from backtest.unified_backtest import aggregate_metrics
    from backtest.universe_shrink import prepare_for_shrink
    closes = {'AAA': _closes(), 'DDD': _closes(step=-0.004)}
    trades = []
    for d in range(1, 21):
        trades.append(dict(_trade('AAA', f'2024-02-{d:02d}', pnl=0.01 + 0.002 * (d % 5)),
                           direction='LONG',
                           exit_date=pd.Timestamp(f'2024-02-{d:02d}').date()
                           + pd.Timedelta(days=6).to_pytimedelta()))
        trades.append(dict(_trade('DDD', f'2024-03-{d:02d}', pnl=-0.01 - 0.002 * (d % 3)),
                           direction='SHORT',
                           exit_date=pd.Timestamp(f'2024-03-{d:02d}').date()
                           + pd.Timedelta(days=5).to_pytimedelta()))
    prepared = prepare_for_shrink(trades, closes.get)
    assert all('daily_marks' in t for t in prepared)
    res = shrink_and_select(prepared, SNAPS, MEMBERS, _regimes_series(), DAY_FREQ)
    expect = aggregate_metrics(prepared)['sharpe']
    assert res['totals_by_tier'][FULL_TIER]['sharpe'] == expect
    # ... and it is NOT the smear number (marks actually changed the result)
    smear = aggregate_metrics(trades)['sharpe']
    assert expect != smear


def test_parity_check_tolerance_and_nulls():
    from backtest.universe_shrink import parity_check
    assert parity_check(0.66, 0.70, 0.10) is True
    assert parity_check(0.66, 0.77, 0.10) is False
    assert parity_check(0.30, 0.66, 0.10) is False
    assert parity_check(None, 0.66, 0.10) is True
    assert parity_check(0.66, None, 0.10) is True
    assert parity_check(float('nan'), 0.66, 0.10) is True
    assert parity_check(0.66, float('inf'), 0.10) is True
    assert parity_check(0.5, 0.5, 0.0) is True
