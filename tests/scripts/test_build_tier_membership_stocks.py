"""Membership artifact builder: stocks_* tiers added, old tiers byte-identical,
latest-known security type on earlier dates, profile-cache fallback."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import build_tier_membership as btm
from src.strategies.universe_meta import TickerMetadata, overlay_security_types

BASE = dict(asset_class='us_equity', exchange='NYSE', status='active', tradable=True,
            shortable=True, fractionable=True, easy_to_borrow=True, market_cap=None,
            adv_usd_20d=None, sector=None, industry=None, options_eligible=False,
            in_sp500=False, in_r1000=False, in_r3000=False, listed_date=None,
            delisted_date=None)


class R:
    def __init__(self, sym, **over):
        self.symbol = sym
        self.metadata = TickerMetadata(**{**BASE, 'symbol': sym, **over})


def _rows(snap_tag=''):
    return [R('AAPL', in_sp500=True, in_r1000=True, in_r3000=True),
            R('BRK.B', in_sp500=True, in_r1000=True, in_r3000=True),   # no profile, in sp500
            R('SPY', in_sp500=True, in_r1000=True, in_r3000=True),     # ETF in index tiers
            R('BIL', in_r3000=True),
            R('PREF', in_r3000=True),                                    # unknown, not sp500
            R('LIQ'), R('DEAD', tradable=False),
            R('FUND', in_r1000=True)]


class Floor:
    def has_floor(self, s, d): return True


def _old_impl(rows, as_of, coverage):
    """Verbatim copy of tiers_for_rows before this change."""
    from src.strategies import universe_default as ud
    tiers = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
    preds = {t: getattr(ud, t) for t in tiers}
    out = {t: [] for t in tiers}
    for row in rows:
        meta = row.metadata
        if not coverage.has_floor(meta.symbol, as_of):
            continue
        for t, p in preds.items():
            try:
                if p(meta, as_of):
                    out[t].append(meta.symbol)
            except Exception:
                continue
    return {t: sorted(v) for t, v in out.items()}


TYPES = {'AAPL': 'stock', 'SPY': 'etf', 'BIL': 'etf', 'FUND': 'fund'}


def test_old_tiers_byte_identical_and_new_tiers_present():
    d = date(2024, 1, 31)
    plain = btm.tiers_for_rows(_rows(), d, Floor())
    typed = btm.tiers_for_rows(overlay_security_types(_rows(), TYPES), d, Floor())
    ref = _old_impl(_rows(), d, Floor())
    for t in btm.LADDER_TIERS:
        assert plain[t] == ref[t] == typed[t]
        assert repr(plain[t]) == repr(ref[t])
    assert set(plain) == set(btm.ALL_TIERS)
    # typed: funds out, BRK.B (unknown + sp500) in, PREF (unknown, not sp500) out
    assert typed['stocks_sp500'] == ['AAPL', 'BRK.B']
    assert typed['stocks_r3000'] == ['AAPL', 'BRK.B']
    assert 'PREF' not in typed['stocks_liquid'] and 'SPY' not in typed['stocks_liquid']
    assert 'LIQ' not in typed['stocks_liquid']          # unknown + not sp500 -> excluded
    # untyped (pre-overlay): only sp500 members survive
    assert plain['stocks_sp500'] == ['AAPL', 'BRK.B', 'SPY']
    for new, old in (('stocks_sp500', 'sp500'), ('stocks_r1000', 'tier_r1000'),
                     ('stocks_r3000', 'tier_r3000'), ('stocks_liquid', 'tier_liquid')):
        assert set(typed[new]) <= set(typed[old])


def test_latest_type_applies_to_earlier_snapshot_dates():
    # The DB overlay is the LATEST type; an early snapshot row (never typed itself)
    # gets it too: SPY is excluded at 2021 and 2024 alike.
    for d in (date(2021, 7, 31), date(2024, 1, 31)):
        got = btm.tiers_for_rows(overlay_security_types(_rows(), TYPES), d, Floor())
        assert 'SPY' not in got['stocks_sp500'] and 'SPY' in got['sp500']


def test_overlay_db_wins_profile_fills_null_column():
    profiles = {'SPY': {'isEtf': True, 'isFund': False, 'isAdr': False},
                'AAPL': {'isEtf': False, 'isFund': False, 'isAdr': False},
                'TSM': {'isEtf': False, 'isFund': False, 'isAdr': True},
                'BRK.B': {'_empty': True}}
    # DB column still NULL everywhere (first weekly snapshot not yet run) -> {}
    ov = btm.build_security_type_overlay({}, profiles)
    assert ov == {'SPY': 'etf', 'AAPL': 'stock', 'TSM': 'adr'}
    # DB value wins where both exist
    assert btm.build_security_type_overlay({'AAPL': 'adr'}, profiles)['AAPL'] == 'adr'
    # fallback alone yields the ETF exclusion
    got = btm.tiers_for_rows(overlay_security_types(_rows(), ov), date(2024, 1, 31), Floor())
    assert 'SPY' not in got['stocks_sp500'] and 'AAPL' in got['stocks_sp500']


def test_load_profile_cache_tolerates_missing(tmp_path):
    assert btm.load_profile_cache(tmp_path / 'nope.json') == {}
    p = tmp_path / 'p.json'; p.write_text('{"A": {"isEtf": true}}')
    assert btm.load_profile_cache(p) == {'A': {'isEtf': True}}
