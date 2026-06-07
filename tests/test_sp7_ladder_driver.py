"""SP-7 Phase B Task 10 — ladder driver queue logic (fake runner)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import run_universe_ladder as drv


def test_cell_priority_extremes_first():
    assert drv.TIER_PRIORITY == {'sp500': 0, 'tier_liquid': 1,
                                 'tier_r1000': 2, 'tier_r3000': 3}


def test_budget_for():
    assert drv.budget_for('S_tr_03_bocpd_change_point') == 21600
    assert drv.budget_for('S_pairs_trading_jump_diffusion_intraday') == 21600
    assert drv.budget_for('anything_else') == 7200


def test_degenerate_detection():
    cells = {'sp500': {'status': 'done', 'trade_sha': 'abc'},
             'tier_liquid': {'status': 'done', 'trade_sha': 'abc'}}
    assert drv.is_degenerate(cells) is True
    cells['tier_liquid']['trade_sha'] = 'xyz'
    assert drv.is_degenerate(cells) is False
    cells['tier_liquid'] = {'status': 'error', 'trade_sha': None}
    assert drv.is_degenerate(cells) is False  # error ≠ identical


def test_consecutive_error_policy():
    assert drv.should_fail_strategy(['error', 'error', 'error']) is True
    assert drv.should_fail_strategy(['error', 'done', 'error']) is False
    assert drv.should_fail_strategy(['error', 'error']) is False


def test_finalize_payload_winner_change():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'done', 'metrics': {'sharpe': 1.2, 'trades_n': 100}, 'w': W},
        'tier_r3000':  {'status': 'timeout', 'metrics': None, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 1.1, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'change' and p['choice'] == 'tier_r1000'
    assert p['summary']['grid'][0]['name'] == 'sp500'
    assert p['summary']['cell_statuses']['tier_r3000'] == 'timeout'


def test_finalize_payload_degenerate():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'skipped_degenerate', 'metrics': None, 'w': W},
        'tier_r3000':  {'status': 'skipped_degenerate', 'metrics': None, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'universe-independent' and p['choice'] == 'sp500'


def test_finalize_payload_no_signal():
    W = ('2021-07-01', '2026-06-05')
    cells = {t: {'status': 'error', 'metrics': None, 'w': W}
             for t in drv.LADDER_TIERS}
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'no_signal' and p['choice'] == 'sp500'


def test_finalize_payload_no_change():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.5, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'done', 'metrics': {'sharpe': 1.55, 'trades_n': 100}, 'w': W},
        'tier_r3000':  {'status': 'done', 'metrics': {'sharpe': 1.2, 'trades_n': 100}, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 0.9, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'no_change' and p['choice'] == 'sp500'


def test_metrics_to_grid_row():
    m = {'sharpe': 1.2, 'max_dd_pct': 10.0, 'win_rate': 0.5, 'trades_n': 50,
         'sortino': 1.5, 'calmar': 1.0, 'mean_holding_days': 3.0,
         'mean_universe_size': 900.0, 'trade_sha': 'x', 'mode': 'tier',
         'candidate': 'tier_r1000'}
    row = drv.grid_row('tier_r1000', m)
    assert row['name'] == 'tier_r1000' and row['sharpe'] == 1.2
    assert drv.grid_row('sp500', None)['sharpe'] is None
