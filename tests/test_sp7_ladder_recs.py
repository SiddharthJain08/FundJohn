"""SP-7 Phase B Task 9 — rec rows + Discord formatting contracts."""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import universe_ladder_recs as recs


GRID = [
    {'name': 'sp500', 'sharpe': 1.0, 'max_dd_pct': 12.0, 'win_rate': 0.55,
     'trades_n': 120, 'sortino': 1.4, 'calmar': 0.9, 'mean_holding_days': 4.0,
     'mean_universe_size': 350.0},
    {'name': 'tier_liquid', 'sharpe': 1.2, 'max_dd_pct': 14.0, 'win_rate': 0.54,
     'trades_n': 300, 'sortino': 1.6, 'calmar': 1.0, 'mean_holding_days': 4.2,
     'mean_universe_size': 4100.0},
]


def test_change_message_has_required_footer():
    msg = recs.format_change_message('momentum_12_1', 'sp500', 'tier_liquid',
                                     'displaced: Δ=+0.20', GRID, rec_id=987)
    assert msg.rstrip().endswith('_footer: universe-rec:987_')
    assert re.search(r'universe-rec:(\d+)', msg).group(1) == '987'
    assert '| `sp500` |' in msg and '| `tier_liquid` |' in msg
    assert len(msg) <= 1900


def test_summary_message_has_no_footer():
    msg = recs.format_summary_message([
        ('s1', 'no_signal'), ('s2', 'universe-independent'), ('s3', 'no_change')])
    assert 'universe-rec:' not in msg
    assert 's1' in msg and len(msg) <= 1900


def test_rationale_is_deterministic():
    v = {'verdict': 'winner', 'choice': 'tier_r3000',
         'eligible': ['sp500', 'tier_r3000'],
         'comparisons': [{'challenger': 'tier_r3000', 'incumbent': 'sp500',
                          'delta': 0.15, 'displaced': True}]}
    r1 = recs.build_rationale(v, window=('2021-07-01', '2026-06-05'))
    r2 = recs.build_rationale(v, window=('2021-07-01', '2026-06-05'))
    assert r1 == r2 and 'tier_r3000' in r1 and '0.15' in r1


def test_autoadopted_message_has_banner_and_no_reaction_footer():
    msg = recs.format_autoadopted_message(
        'momentum_12_1', 'sp500', 'tier_liquid',
        'displaced: Δ=+0.20', GRID, rec_id=987)
    assert 'AUTO-ADOPTED' in msg
    assert 'universe-rec:' not in msg          # reaction parser must NOT fire
    assert 'React ✅' not in msg
    assert '| `sp500` |' in msg and len(msg) <= 1900
    assert 'rec 987' in msg                    # id still visible for audit
