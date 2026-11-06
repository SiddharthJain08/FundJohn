import urllib.error

import pytest

from src.ingestion import _http_retry


def test_redact_url_masks_known_secret_params():
    for u in ('https://mainnet.helius-rpc.com/?api-key=SECRET',
              'https://api.blockcypher.com/v1/doge/main?limit=50&token=SECRET',
              'https://api.blockchair.com/bitcoin-cash/transactions?q=x&key=SECRET',
              'https://api.etherscan.io/v2/api?chainid=1&apikey=SECRET'):
        r = _http_retry.redact_url(u)
        assert '***' in r and 'SECRET' not in r
    assert _http_retry.redact_url('https://x.io/v1?limit=50&token=S') == 'https://x.io/v1?limit=50&token=***'
    unchanged = 'https://fapi.binance.com/fapi/v1/ticker?symbol=BTCUSDT'
    assert _http_retry.redact_url(unchanged) == unchanged
    assert _http_retry.redact_url('https://x.io/path') == 'https://x.io/path'
    assert _http_retry.redact_url('not a url [') == 'not a url ['


@pytest.mark.parametrize('exc', [
    lambda u: urllib.error.HTTPError(u, 500, 'boom', {}, None),
    lambda u: TimeoutError('t'),
], ids=['http_500', 'timeout'])
def test_fetch_with_retry_final_failure_log_is_redacted(monkeypatch, capsys, exc):
    url = 'https://mainnet.helius-rpc.com/?api-key=SECRET123'

    def boom(u, timeout=None):
        raise exc(url)
    monkeypatch.setattr(_http_retry, 'urlopen', boom)
    assert _http_retry.fetch_with_retry(url, attempts=1, label='t') is None
    err = capsys.readouterr().err
    assert 'SECRET123' not in err and '***' in err
