"""SP-1: bounded yfinance interface for CBOE vol indices."""
from unittest.mock import patch
import pandas as pd
import pytest

from src.ingestion.cboe_vol_indices import (
    get_vix, get_vvix, get_vix3m, get_vix9d,
)


@patch('src.ingestion.cboe_vol_indices._yf_download')
def test_get_vix_returns_dataframe_with_close(mock_dl):
    mock_dl.return_value = pd.DataFrame(
        {'Close': [18.2, 18.5]},
        index=pd.to_datetime(['2026-05-20', '2026-05-21']),
    )
    df = get_vix()
    assert 'Close' in df.columns or 'close' in df.columns
    assert len(df) == 2


@patch('src.ingestion.cboe_vol_indices._yf_download')
def test_get_vvix_calls_yf_with_correct_ticker(mock_dl):
    mock_dl.return_value = pd.DataFrame({'Close': [90.1]}, index=pd.to_datetime(['2026-05-21']))
    get_vvix()
    args, _ = mock_dl.call_args
    assert args[0] == '^VVIX'


@patch('src.ingestion.cboe_vol_indices._yf_download', side_effect=[RuntimeError('first try'), pd.DataFrame({'Close': [18.0]}, index=pd.to_datetime(['2026-05-21']))])
def test_get_vix_single_retry_on_transient_error(mock_dl):
    df = get_vix()
    assert len(df) == 1
    assert mock_dl.call_count == 2


@patch('src.ingestion.cboe_vol_indices._yf_download', side_effect=RuntimeError('persistent'))
def test_two_failures_raise(mock_dl):
    with pytest.raises(RuntimeError):
        get_vix()
