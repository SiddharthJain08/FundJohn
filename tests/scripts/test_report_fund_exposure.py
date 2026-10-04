"""report_fund_exposure pure parts: classification, share, rows, formatting."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import report_fund_exposure as rep

P = {'SPY': {'isEtf': True, 'isFund': False, 'isAdr': False},
     'VFIAX': {'isEtf': False, 'isFund': True, 'isAdr': False},
     'AAPL': {'isEtf': False, 'isFund': False, 'isAdr': False},
     'TSM': {'isEtf': False, 'isFund': False, 'isAdr': True},
     'BRK.B': {'_empty': True}, 'BRK-B': {'isEtf': False, 'isFund': False, 'isAdr': False}}


def test_ticker_security_type_with_hyphen_fallback():
    assert rep.ticker_security_type('SPY', P) == 'etf'
    assert rep.ticker_security_type('BRK.B', P) == 'stock'
    assert rep.ticker_security_type('NOPE', P) is None


def test_fund_share_counts_etf_and_fund_not_unknown_or_adr():
    s = rep.fund_share(['SPY', 'SPY', 'VFIAX', 'AAPL', 'TSM', 'NOPE'], P)
    assert s == {'total': 6, 'fund': 3, 'unknown': 1, 'share': 0.5}
    assert rep.fund_share([], P)['share'] is None


def test_build_rows_filters_states_and_sorts():
    manifest = {'strategies': {
        'A': {'state': 'live', 'metadata': {'universe_filter_ref': 'm:tier_liquid',
                                            'backtest_universe_cap': 'tier_liquid'}},
        'B': {'state': 'candidate', 'metadata': {}},
        'C': {'state': 'deprecated', 'metadata': {}},
        'D': {'state': 'live', 'metadata': {'universe_filter_ref': 'x.y:sp500'}}}}
    rows = rep.build_rows(manifest, {'live', 'candidate'},
                          {'A': ['SPY', 'AAPL'], 'D': ['AAPL']}, P, {'A': True})
    assert [r['strategy_id'] for r in rows] == ['A', 'D', 'B']   # share desc, no-trades last
    a = rows[0]
    assert (a['fund_share'], a['universe_filter_ref'], a['backtest_universe_cap'],
            a['any_regime_active']) == (0.5, 'tier_liquid', 'tier_liquid', True)
    assert rows[1]['any_regime_active'] is False and rows[2]['fund_share'] is None
    json.dumps(rows)


def test_format_table_min_share_and_na():
    rows = [{'strategy_id': 'A', 'state': 'live', 'trades': 4, 'fund_trades': 2,
             'unknown_trades': 0, 'fund_share': 0.5, 'universe_filter_ref': 'sp500',
             'backtest_universe_cap': None, 'any_regime_active': True},
            {'strategy_id': 'B', 'state': 'live', 'trades': 0, 'fund_trades': 0,
             'unknown_trades': 0, 'fund_share': None, 'universe_filter_ref': None,
             'backtest_universe_cap': None, 'any_regime_active': False},
            {'strategy_id': 'C', 'state': 'live', 'trades': 100, 'fund_trades': 1,
             'unknown_trades': 0, 'fund_share': 0.01, 'universe_filter_ref': 'sp500',
             'backtest_universe_cap': None, 'any_regime_active': False}]
    t = rep.format_table(rows, min_share=0.05)
    assert '50.0' in t and 'n/a' in t and '\nC ' not in t
