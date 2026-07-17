"""SP-7 Phase B Task 5 — tier membership precompute (pure logic)."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import build_tier_membership as btm


def test_snapshot_dates():
    ds = btm.snapshot_dates(date(2021, 7, 1), date(2021, 9, 15))
    assert ds == [date(2021, 7, 31), date(2021, 8, 31), date(2021, 9, 15)]


def test_coverage_matrix_cumulative_floor():
    prices = pd.DataFrame({
        'ticker': ['A'] * 70 + ['B'] * 10,
        'date': [f'2021-{1 + i // 28:02d}-{1 + i % 28:02d}' for i in range(70)]
               + [f'2021-01-{i + 1:02d}' for i in range(10)],
    })
    cov = btm.CoverageIndex(prices, min_bars=60)
    # A has 70 bars by 2021-03-12; B never reaches 60
    assert cov.has_floor('A', date(2021, 3, 31)) is True
    assert cov.has_floor('A', date(2021, 1, 31)) is False   # only ~28 bars
    assert cov.has_floor('B', date(2021, 12, 31)) is False
    assert cov.has_floor('ZZZ', date(2021, 12, 31)) is False


def test_membership_nesting(monkeypatch):
    """Tiers built from the same rows must nest (predicates force it)."""
    from src.strategies.universe_meta import TickerMetadata

    def _m(sym, **over):
        base = dict(symbol=sym, asset_class='us_equity', exchange='NYSE',
                    status='active', tradable=True, shortable=True,
                    fractionable=True, easy_to_borrow=True, market_cap=None,
                    adv_usd_20d=None, sector=None, industry=None,
                    options_eligible=False, in_sp500=False, in_r1000=False,
                    in_r3000=False, listed_date=None, delisted_date=None)
        base.update(over)
        class R: pass
        r = R(); r.metadata = TickerMetadata(**base); r.symbol = sym
        return r

    rows = [_m('SPX1', in_sp500=True), _m('R1', in_r1000=True),
            _m('R3', in_r3000=True), _m('LIQ'),
            _m('DEAD', tradable=False)]

    class AllFloor:
        def has_floor(self, s, d): return True

    members = btm.tiers_for_rows(rows, date(2024, 1, 31), AllFloor())
    assert set(members['sp500']) <= set(members['tier_r1000'])
    assert set(members['tier_r1000']) <= set(members['tier_r3000'])
    assert set(members['tier_r3000']) <= set(members['tier_liquid'])
    assert 'DEAD' not in members['tier_liquid']
    assert members['tier_liquid'] == sorted(members['tier_liquid'])
