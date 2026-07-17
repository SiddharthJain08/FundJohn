"""SP-7 Phase B Task 1 — tier predicates: nesting by construction."""
from __future__ import annotations
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.strategies.universe_meta import TickerMetadata
from src.strategies import universe_default as ud


def _meta(**over):
    base = dict(
        symbol='TEST', asset_class='us_equity', exchange='NASDAQ',
        status='active', tradable=True, shortable=True, fractionable=True,
        easy_to_borrow=True, market_cap=None, adv_usd_20d=None,
        sector=None, industry=None, options_eligible=False,
        in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=None, delisted_date=None,
    )
    base.update(over)
    return TickerMetadata(**base)


def test_liquid_tradable_definition():
    assert ud.liquid_tradable(_meta(), None) is True
    assert ud.liquid_tradable(_meta(tradable=False), None) is False
    assert ud.liquid_tradable(_meta(status='inactive'), None) is False
    assert ud.liquid_tradable(_meta(easy_to_borrow=False), None) is False


def test_tier_unions():
    # sp500-only name is in every tier
    m = _meta(in_sp500=True, tradable=False, easy_to_borrow=False)
    assert ud.sp500(m, None) and ud.tier_r1000(m, None)
    assert ud.tier_r3000(m, None) and ud.tier_liquid(m, None)
    # r1000-only
    m = _meta(in_r1000=True, tradable=False, easy_to_borrow=False)
    assert not ud.sp500(m, None) and ud.tier_r1000(m, None) and ud.tier_r3000(m, None)
    # liquid-only (not in any index)
    m = _meta()
    assert not ud.tier_r3000(m, None) and ud.tier_liquid(m, None)


def test_nesting_property_exhaustive():
    """For EVERY combination of the 6 driving booleans, nesting holds."""
    for sp, r1, r3, tr, etb, act in itertools.product([True, False], repeat=6):
        m = _meta(in_sp500=sp, in_r1000=r1, in_r3000=r3,
                  tradable=tr, easy_to_borrow=etb,
                  status='active' if act else 'inactive')
        chain = [ud.sp500(m, None), ud.tier_r1000(m, None),
                 ud.tier_r3000(m, None), ud.tier_liquid(m, None)]
        for narrow, broad in zip(chain, chain[1:]):
            assert (not narrow) or broad, f'nesting violated for {m}'


def test_candidate_predicates_registered():
    for name in ('liquid_tradable', 'tier_r1000', 'tier_r3000', 'tier_liquid'):
        assert name in ud.CANDIDATE_PREDICATES
    # legacy 12 untouched
    assert len(ud.CANDIDATE_PREDICATES) == 16
