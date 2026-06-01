#!/usr/bin/env python3
"""Counterfactual backtest: stacked bracket vs. legacy max-weight pick.

Replays historical co-firing events from execution_signals; for each event it
builds BOTH brackets (current _select_bracket pick and stacked_bracket), runs
unified_backtest.simulate_trade on the ticker's forward bars for each, and
aggregates per-trade P&L / hit-rate / exit-reason mix. This isolates the
bracket-policy effect (same entries, same tickers, different exits).

DECISION-GRADE PARITY (2026-05-29):
  * Candidates are FILTERED to the current live set (strategy_weights.load_current
    for the regime) and each carries its real `daily_weight` — so the "current"
    policy is production's true max-daily_weight pick, not a strategy_id-ordered
    tie-break. Both the candidate SET and the weights match production.
  * The multi-block subset is reported separately: that is where stacking acts.
    On single-block tickers the only difference is top-sharpe-rep vs max-weight
    selection.

LIMITATIONS (documented, by design):
  * Size held fixed (per-unit) -> measures per-trade exit quality, not portfolio
    interaction.
  * CURRENT orthogonalization substrate (load_groups) + current daily_weight /
    effective_sharpe are used for ALL historical events (point-in-time substrate
    is not reconstructed). LOW_VOL is the live regime; do NOT sweep other regimes
    (applying a regime's weights to events that aren't regime-filtered is a
    confound). For robustness, split by TIME (this script reports an early/late
    half split of the multi-block subset).
  * Tier-1 FOLD is NOT pre-applied. This is precise, not hand-wavy:
      - stacked_bracket is fold-invariant (top-sharpe-of-block == top-sharpe of
        the fold-reps; the per-block TP stack is unchanged by within-group collapse),
      - the multi-block COUNT is fold-invariant (fold-groups subset blocks),
      so fold affects ONLY the *current* baseline's bracket pick — specifically the
      STOP (TP is ~flat 5%) when the max-daily_weight raw candidate is not the
      fold-rep within a multi-member block. The gap is on the side we compare against.
  * current vs stacked may anchor different entry_price values when co-firing
    strategies wrote different entries; pnl_pct is relative so the comparison holds.
Read-only. Touches no master data.

Usage: python3 scripts/backtest_bracket_stacking.py [--days N] [--regime LOW_VOL] [--max-hold 10]
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from execution.regime_blended_sizer import _select_bracket, _dir_to_int  # noqa: E402
from execution import bracket_stacking as bs  # noqa: E402
from backtest.unified_backtest import simulate_trade  # noqa: E402


def compare_event(ticker, dir_sign, candidates, bars, entry_date,
                  block_map, eff_sharpe, max_hold_days):
    """Run simulate_trade for both bracket policies on one event.
    Returns {'current': exit_dict|None, 'stacked': exit_dict|None}.
    Candidates must already carry their real daily_weight in 'weight'."""
    cur = _select_bracket(candidates, dir_sign)
    stk = bs.stacked_bracket(candidates, dir_sign, block_map, eff_sharpe)
    out = {'current': None, 'stacked': None}
    for key, br in (('current', cur), ('stacked', stk)):
        if not br or br.get('entry') is None or br.get('stop') is None or br.get('t1') is None:
            continue
        out[key] = simulate_trade(
            bars=bars, entry_date=entry_date, direction=dir_sign,
            entry_price=float(br['entry']), stop_loss=float(br['stop']),
            target_1=float(br['t1']), max_hold_days=max_hold_days)
    return out


def _load_substrate(regime):
    """Returns (block_map, eff_sharpe, weight_by_strat) for the regime's current
    live weights — the production active set + sizing weights."""
    from execution import strategy_similarity as ss
    from execution import strategy_weights as sw
    groups = ss.load_groups(regime)
    rows = sw.load_current(regime)
    eff_sharpe = {r['strategy_id']: float(r['effective_sharpe']) for r in rows}
    weight_by_strat = {r['strategy_id']: float(r['daily_weight']) for r in rows}
    return groups.get('block_map', {}), eff_sharpe, weight_by_strat


def _load_events(days):
    """Co-firing events: {(signal_date, ticker): [bracket_dict, ...]} with >=1 finite bracket."""
    import psycopg2, psycopg2.extras
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('''
            SELECT DISTINCT ON (signal_date, strategy_id, ticker)
                   signal_date, strategy_id, ticker, direction,
                   entry_price, stop_loss, target_1, target_2
            FROM execution_signals
            WHERE signal_date >= CURRENT_DATE - make_interval(days => %s)
            ORDER BY signal_date, strategy_id, ticker
        ''', (days,))
        rows = cur.fetchall()
    finally:
        conn.close()
    events = defaultdict(list)
    for r in rows:
        events[(r['signal_date'], r['ticker'])].append({
            'sid': r['strategy_id'], 'direction': _dir_to_int(r['direction']),
            'weight': 1.0, 'entry': r['entry_price'], 'stop': r['stop_loss'],
            't1': r['target_1'], 't2': r['target_2'],
        })
    return events


def _new_bucket():
    return {'n': 0, 'pnl': {'current': 0.0, 'stacked': 0.0},
            'hold': {'current': 0.0, 'stacked': 0.0},
            'reasons': {'current': defaultdict(int), 'stacked': defaultdict(int)},
            'diffs': []}


def _accumulate(bucket, res):
    bucket['n'] += 1
    for key in ('current', 'stacked'):
        bucket['pnl'][key] += res[key]['pnl_pct']
        bucket['hold'][key] += res[key]['holding_days']
        bucket['reasons'][key][res[key]['exit_reason']] += 1
    bucket['diffs'].append(res['stacked']['pnl_pct'] - res['current']['pnl_pct'])


def _paired_stats(diffs):
    """Per-event paired Δ(stacked-current): bootstrap 95% CI on the mean (robust
    to the skewed, high-stop-rate distribution), win-rate, and edge concentration
    (share of the summed Δ contributed by the top-10 events — >100% means a few
    outliers carry it while the rest are net-negative)."""
    a = np.asarray(diffs, dtype=float)
    n = len(a)
    if n == 0:
        return None
    rng = np.random.default_rng(12345)        # fixed seed -> reproducible
    boot = a[rng.integers(0, n, size=(2000, n))].mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    total = a.sum()
    top10 = np.sort(a)[::-1][:10].sum()
    return {'mean': float(a.mean()), 'lo': float(lo), 'hi': float(hi),
            'win': float((a > 0).mean()),
            'top10_share': float(top10 / total) if total != 0 else float('nan')}


def _report(name, b):
    if b['n'] == 0:
        print(f'{name}: 0 events')
        return
    cur_m = b['pnl']['current'] / b['n']
    stk_m = b['pnl']['stacked'] / b['n']
    delta = stk_m - cur_m
    print(f'\n{name}  (n={b["n"]})')
    print(f'  current   mean_pnl/trade={cur_m:+.4f}  avg_hold={b["hold"]["current"]/b["n"]:5.2f}d  '
          f'exits={dict(b["reasons"]["current"])}')
    print(f'  stacked   mean_pnl/trade={stk_m:+.4f}  avg_hold={b["hold"]["stacked"]/b["n"]:5.2f}d  '
          f'exits={dict(b["reasons"]["stacked"])}')
    print(f'  stacked - current: {delta:+.4f} mean pnl/trade')
    ps = _paired_stats(b['diffs'])
    if ps:
        sig = 'CI excludes 0' if (ps['lo'] > 0 or ps['hi'] < 0) else 'CI includes 0 (not distinguishable)'
        print(f'  paired Δ: mean={ps["mean"]:+.4f}  boot95%CI=[{ps["lo"]:+.4f},{ps["hi"]:+.4f}] ({sig})  '
              f'win_rate={ps["win"]:.0%}  top10_share_of_edge={ps["top10_share"]:.0%}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--regime', default='LOW_VOL')
    ap.add_argument('--max-hold', type=int, default=10)
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

    from backtest.unified_backtest import load_prices_panels
    _, panels = load_prices_panels()
    block_map, eff_sharpe, weight_by_strat = _load_substrate(args.regime)
    events = _load_events(args.days)
    # Split at the midpoint of the ACTUAL data range (not the window), so the
    # early/late robustness cut is balanced even when the window exceeds the
    # available signal history.
    _ev_dates = [pd.Timestamp(d) for (d, _t) in events] or [pd.Timestamp(date.today())]
    mid_date = min(_ev_dates) + (max(_ev_dates) - min(_ev_dates)) / 2

    buckets = {'all': _new_bucket(), 'multiblock': _new_bucket(),
               'multiblock_early': _new_bucket(), 'multiblock_late': _new_bucket()}
    n_events_total = len(events)
    n_dropped_no_live = 0   # events where no current-live strategy fired
    n_no_bars = 0
    for (sig_date, ticker), raw_cands in events.items():
        # Production parity: only current-live strategies, carrying real daily_weight.
        cands = [c for c in raw_cands if c['sid'] in weight_by_strat]
        if not cands:
            n_dropped_no_live += 1
            continue
        for c in cands:
            c['weight'] = weight_by_strat[c['sid']]
        net = sum(c['direction'] for c in cands)
        if net == 0:
            continue
        dir_sign = 1 if net > 0 else -1
        bars = panels.get(ticker)
        if bars is None or bars.empty:
            n_no_bars += 1
            continue
        entry_date = pd.Timestamp(sig_date)
        n_blocks = len({block_map.get(c['sid'], c['sid']) for c in cands
                        if c['direction'] == dir_sign})
        res = compare_event(ticker, dir_sign, cands, bars, entry_date,
                            block_map, eff_sharpe, args.max_hold)
        if not (res['current'] and res['stacked']):
            continue
        _accumulate(buckets['all'], res)
        if n_blocks >= 2:
            _accumulate(buckets['multiblock'], res)
            half = 'multiblock_early' if entry_date < mid_date else 'multiblock_late'
            _accumulate(buckets[half], res)

    print(f'\n=== bracket-stacking counterfactual (DECISION-GRADE) ===')
    print(f'window={args.days}d  regime={args.regime}  max_hold={args.max_hold}')
    print(f'events_total={n_events_total}  dropped_no_live_strategy={n_dropped_no_live}  '
          f'dropped_no_bars={n_no_bars}  events_used={buckets["all"]["n"]}')
    print(f'live strategies in {args.regime} weight set: {len(weight_by_strat)}')
    if buckets['all']['n'] == 0:
        print('no comparable events.')
        return
    _report('ALL events', buckets['all'])
    _report('MULTI-BLOCK subset (where stacking acts)', buckets['multiblock'])
    _report('  multi-block EARLY half', buckets['multiblock_early'])
    _report('  multi-block LATE half', buckets['multiblock_late'])
    if buckets['multiblock']['n'] < 30:
        print('\n[!] multi-block n is small — treat the multi-block delta as noisy.')


if __name__ == '__main__':
    main()
