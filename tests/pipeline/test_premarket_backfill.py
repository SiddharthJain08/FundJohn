import pytest
from datetime import date
from unittest.mock import patch, MagicMock
from scripts.backfill_premarket_realized_pnl import backfill_rows, _compute_pnl


def _fake_bars(open_t, close_t, open_tplus1):
    return {'open': open_t, 'close': close_t, 'open_next': open_tplus1}


def test_compute_pnl_returns_expected_percents():
    pnl = _compute_pnl(open_t=100.0, close_t=95.0, open_tplus1=92.0)
    assert pnl['open_to_close'] == pytest.approx(-0.05)
    assert pnl['open_to_open']  == pytest.approx(-0.08)


@patch('scripts.backfill_premarket_realized_pnl._fetch_bars_for')
@patch('scripts.backfill_premarket_realized_pnl._fetch_unfilled_alerts')
@patch('scripts.backfill_premarket_realized_pnl._write_pnl_back')
def test_backfill_skips_already_filled_rows(mock_write, mock_fetch_unfilled, mock_bars):
    mock_fetch_unfilled.return_value = []
    backfill_rows()
    mock_bars.assert_not_called()
    mock_write.assert_not_called()


@patch('scripts.backfill_premarket_realized_pnl._fetch_bars_for')
@patch('scripts.backfill_premarket_realized_pnl._fetch_unfilled_alerts')
@patch('scripts.backfill_premarket_realized_pnl._write_pnl_back')
def test_backfill_writes_only_when_bars_available(
    mock_write, mock_fetch_unfilled, mock_bars,
):
    mock_fetch_unfilled.return_value = [
        {'id': 1, 'ticker': 'GLW', 'trading_day': date(2026, 5, 28)},
        {'id': 2, 'ticker': 'AAPL','trading_day': date(2026, 5, 28)},
    ]
    mock_bars.side_effect = [
        _fake_bars(100.0, 95.0, 92.0),
        None,
    ]
    backfill_rows()
    assert mock_write.call_count == 1
    assert mock_write.call_args[0][0] == 1
