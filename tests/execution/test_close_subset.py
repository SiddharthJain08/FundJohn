from unittest.mock import patch, MagicMock
from src.execution.regime_liquidator import close_subset


def _fake_positions(by_ticker):
    return [
        {'symbol': sym, 'qty': str(qty), 'asset_class': 'us_equity',
         'market_value': str(abs(qty) * 10)}
        for sym, qty in by_ticker.items()
    ]


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions_list')
def test_close_subset_only_closes_named_tickers(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({
        'GLW': 100, 'AAPL': 50, 'MSFT': -25, 'NVDA': 200,
    })
    mock_submit.return_value = {
        'status': 'filled', 'filled_qty': 100, 'avg_fill_price': 32.10,
    }
    out = close_subset(['GLW', 'MSFT'], reason='PREMARKET_PANIC')

    submitted = [c.args[0]['symbol'] for c in mock_submit.call_args_list]
    assert set(submitted) == {'GLW', 'MSFT'}
    assert 'AAPL' not in submitted and 'NVDA' not in submitted
    assert len(out) == 2


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions_list')
def test_close_subset_uses_sell_for_long_and_buy_for_short(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100, 'MSFT': -25})
    mock_submit.return_value = {'status': 'pending'}
    close_subset(['GLW', 'MSFT'], reason='PREMARKET_PANIC')

    by_symbol = {c.args[0]['symbol']: c.args[0] for c in mock_submit.call_args_list}
    assert by_symbol['GLW']['side'] == 'sell'
    assert by_symbol['GLW']['qty'] == 100
    assert by_symbol['MSFT']['side'] == 'buy'
    assert by_symbol['MSFT']['qty'] == 25


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions_list')
def test_close_subset_audits_every_attempt_with_reason(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100})
    mock_submit.return_value = {'status': 'filled', 'filled_qty': 100, 'avg_fill_price': 32.0}

    close_subset(['GLW'], reason='PREMARKET_PANIC')

    audit_args = mock_audit.call_args[0][0]
    assert audit_args['ticker'] == 'GLW'
    assert audit_args['regime_from'] == 'PREMARKET_PANIC'
    assert audit_args['regime_to'] == 'FLAT'
    assert audit_args['result_status'] == 'filled'


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions_list')
def test_close_subset_continues_on_per_ticker_failure(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100, 'AAPL': 50})
    mock_submit.side_effect = [
        RuntimeError('alpaca CLI: halted'),
        {'status': 'filled', 'filled_qty': 50, 'avg_fill_price': 200.0},
    ]
    out = close_subset(['GLW', 'AAPL'], reason='PREMARKET_PANIC')

    assert len(out) == 2
    statuses = {r['ticker']: r['status'] for r in out}
    assert statuses['GLW'] == 'submit_error'
    assert statuses['AAPL'] == 'filled'
    assert mock_audit.call_count == 2


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions_list')
def test_close_subset_skips_unknown_ticker(mock_load, mock_submit, mock_audit):
    mock_load.return_value = _fake_positions({'GLW': 100})
    out = close_subset(['GLW', 'NOSUCH'], reason='PREMARKET_PANIC')

    assert mock_submit.call_count == 1
    statuses = {r['ticker']: r['status'] for r in out}
    assert statuses['GLW'] != 'submit_error'
    assert statuses['NOSUCH'] == 'no_position'
