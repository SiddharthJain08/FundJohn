"""tests/execution/test_handoff_portfolio_value_live.py

The structured handoff's `portfolio.portfolio_value` must come from the LIVE
broker account, not from output/portfolio.json.

REGRESSION (2026-08-01): trade_handoff_builder.build() augmented the portfolio
block with a live Alpaca snapshot using

    portfolio.setdefault('portfolio_value', _account['equity'])

while its five sibling fields (long_market_value, short_market_value, cash,
buying_power, regt_buying_power) all used plain assignment. output/portfolio.json
ships a hardcoded template `"portfolio_value": 1000000`, so the key was ALWAYS
present, setdefault was a permanent no-op, and every structured handoff carried
$1,000,000 against a real ~$96k equity — a 10.4x overstatement — even as the
`[handoff] live Alpaca: equity=$96,423` line printed two lines below reported
the truth.

Live sizing was never affected (regime_blended_sizer_live takes equity from
_resolve_account_or_none(), a broker fetch); the consumer is
pyportfolioopt_shadow_sizer.py, which never routes orders. The damage was that
every shadow-vs-live comparison ran at ~10x NAV.

These tests drive the REAL build() with every DB/disk/broker seam stubbed —
notably write_handoff, so no output/handoffs/ artifact is touched.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

MOD = 'src.execution.trade_handoff_builder'

STALE_PORTFOLIO = {'portfolio_value': 1_000_000, 'currency': 'USD'}
LIVE_ACCOUNT = {
    'fetched': True,
    'equity': 96_422.74,
    'long_market_value': 9_238.90,
    'short_market_value': 0.0,
    'cash': 87_183.84,
    'buying_power': 373_611.58,
    'regt_buying_power': 183_252.05,
}


@pytest.fixture
def built():
    """Run build() with every side-effecting seam stubbed; return the payload.

    Yields a callable so each test can vary the account. signals=[] keeps the
    feature-enrichment loop (and load_prices) out of the picture entirely —
    build() guards it with `if signals:`.
    """
    def _run(account, portfolio=None):
        captured = {}

        def _capture(run_date, kind, payload):
            captured['payload'] = payload
            return None

        with patch(f'{MOD}.load_signals', return_value=[]), \
             patch(f'{MOD}.load_regime', return_value={'state': 'LOW_VOL'}), \
             patch(f'{MOD}.load_portfolio_state',
                   return_value=dict(STALE_PORTFOLIO if portfolio is None else portfolio)), \
             patch(f'{MOD}.load_veto_history', return_value={}), \
             patch(f'{MOD}.load_mastermind_rec', return_value={}), \
             patch(f'{MOD}.load_yesterdays_vetoed', return_value=[]), \
             patch(f'{MOD}._get_sigma_gate', return_value=2.0), \
             patch(f'{MOD}.load_yesterdays_performance_outliers', return_value=([], [])), \
             patch(f'{MOD}.load_tradable_universe', return_value=set()), \
             patch(f'{MOD}.write_handoff', side_effect=_capture), \
             patch(f'{MOD}.psycopg2.connect', side_effect=RuntimeError('no DB in test')), \
             patch('execution.alpaca_trader._alpaca_session', return_value=object()), \
             patch('execution.alpaca_trader._fetch_account_state', return_value=account):
            __import__(MOD, fromlist=['build']).build('2026-07-30')
        return captured['payload']
    return _run


def test_live_equity_overwrites_stale_portfolio_value(built):
    """THE REGRESSION. A stale 1_000_000 on disk must NOT survive a live fetch."""
    payload = built(LIVE_ACCOUNT)
    pv = payload['portfolio']['portfolio_value']
    assert pv == pytest.approx(96_422.74), (
        f'portfolio_value={pv!r} — the live equity must overwrite the stale '
        'output/portfolio.json template value, not defer to it'
    )
    assert pv != 1_000_000


def test_portfolio_value_agrees_with_its_sibling_fields(built):
    """portfolio_value must be consistent with the cash/long_mv it ships beside.

    The bug was invisible precisely because the siblings WERE live — a reader
    (or the shadow sizer) sees a coherent-looking block whose headline number is
    off by 10x. Equity == cash + long_mv - short_mv for a cash-settled book.
    """
    p = built(LIVE_ACCOUNT)['portfolio']
    assert p['portfolio_value'] == pytest.approx(
        p['cash'] + p['long_market_value'] - p['short_market_value'], rel=1e-6)


def test_stale_value_is_kept_when_the_broker_fetch_fails(built):
    """Fail-soft is preserved: an unfetched account leaves the file value alone.

    The augmentation block is guarded by `if _account.get('fetched')` and wrapped
    in try/except — a broker outage must not blank the portfolio block.
    """
    payload = built({'fetched': False})
    assert payload['portfolio']['portfolio_value'] == 1_000_000


def test_no_portfolio_json_still_gets_live_equity(built):
    """With no file on disk the key is absent — the old setdefault worked here.

    This is the case that made the bug survive review: the code is correct
    whenever output/portfolio.json is missing, and only wrong when it exists.
    """
    payload = built(LIVE_ACCOUNT, portfolio={})
    assert payload['portfolio']['portfolio_value'] == pytest.approx(96_422.74)
