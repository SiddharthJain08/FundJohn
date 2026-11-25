"""2026-11-25: a batch that began in RTH must not drift into the ext-hours
order shape once the close has passed (rth_spillover). See the module comment
above alpaca_executor._BATCH for the incident."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _reset_batch():
    ae._BATCH.update({'session': None, 'close_at': None})
    yield
    ae._BATCH.update({'session': None, 'close_at': None})


def test_begin_batch_rth_uses_clock_next_close_half_day_aware():
    ae._begin_batch('rth', payload={'is_open': True, 'next_close': '2026-11-27T13:00:00-05:00'})
    assert ae._BATCH['session'] == 'rth'
    assert ae._BATCH['close_at'] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_begin_batch_rth_falls_back_to_static_1600_et():
    now = datetime(2026, 11, 25, 20, 55, tzinfo=UTC)   # 15:55 ET
    ae._begin_batch('rth', payload={'is_open': True}, now_utc=now)
    assert ae._BATCH['close_at'] == datetime(2026, 11, 25, 21, 0, tzinfo=UTC)


def test_begin_batch_non_rth_has_no_close():
    ae._begin_batch('afterhours', payload={'is_open': False})
    assert ae._BATCH == {'session': 'afterhours', 'close_at': None}


def test_spillover_only_after_close_and_only_for_rth_batches():
    ae._begin_batch('rth', payload={'next_close': '2026-11-25T16:00:00-05:00'})
    assert ae._rth_spillover(datetime(2026, 11, 25, 20, 59, 59, tzinfo=UTC)) is None
    reason = ae._rth_spillover(datetime(2026, 11, 25, 21, 0, 0, tzinfo=UTC))
    assert reason and 'began in RTH' in reason and '16:00:00 ET passed' in reason
    # an afterhours lane (premarket/afterhours redeploys) is never gated
    ae._begin_batch('afterhours')
    assert ae._rth_spillover(datetime(2026, 11, 25, 21, 30, tzinfo=UTC)) is None
    # unstamped batch (ad-hoc execute_single) is inert
    ae._BATCH.update({'session': None, 'close_at': None})
    assert ae._rth_spillover(datetime(2026, 11, 25, 23, 0, tzinfo=UTC)) is None


def _order(**kw):
    o = {'ticker': 'ACT', 'strategy_id': 'S_TEST', 'direction': 'long',
         'pct_nav': 0.02, 'entry': 45.0, 'stop': 41.0, 't1': 50.0}
    o.update(kw); return o


def test_execute_single_refuses_open_after_close_without_touching_the_broker():
    ae._begin_batch('rth', payload={'next_close': '2026-11-25T16:00:00-05:00'})
    past = datetime(2026, 11, 25, 21, 0, 26, tzinfo=UTC)
    with patch.object(ae, '_rth_spillover', side_effect=lambda now_utc=None: _spill_at(past)), \
         patch('execution.alpaca_executor.subprocess.run') as mock_run:
        res = ae.execute_single(MagicMock(), equity=100_000.0, order=_order(), run_date='2026-11-25')
    assert res['status'] == 'SKIP'
    assert res['reason'].startswith('rth_spillover:')
    assert mock_run.call_count == 0          # no quote, no asset lookup, no submit


def test_execute_single_refuses_close_only_after_close():
    ae._begin_batch('rth', payload={'next_close': '2026-11-25T16:00:00-05:00'})
    past = datetime(2026, 11, 25, 21, 0, 26, tzinfo=UTC)
    with patch.object(ae, '_rth_spillover', side_effect=lambda now_utc=None: _spill_at(past)), \
         patch('execution.alpaca_executor.subprocess.run') as mock_run:
        res = ae.execute_single(MagicMock(), equity=100_000.0,
                                order=_order(close_only=True, notional_usd=0, current_usd=2400.0),
                                run_date='2026-11-25')
    assert res['status'] == 'SKIP' and res['reason'].startswith('rth_spillover:')
    assert mock_run.call_count == 0


def _spill_at(now):
    """Evaluate the real guard at a fixed instant (the stamped batch is read live)."""
    sess, close_at = ae._BATCH.get('session'), ae._BATCH.get('close_at')
    if sess != 'rth' or close_at is None or now < close_at:
        return None
    return f'batch began in RTH, close 16:00:00 ET passed ({now.isoformat()})'
