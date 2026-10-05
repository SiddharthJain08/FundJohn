"""rank_in_r1000_r3000 counts common-stock types only. Fakes only; no DB/HTTP."""
from datetime import date

import pandas as pd
import pytest

from src.pipeline import ticker_metadata_writer as w
from src.pipeline.backfillers import universe_metadata as um
from src.pipeline.backfillers.universe_metadata import rank_in_r1000_r3000
from src.strategies import universe_meta

EXCLUDED = ("etf", "fund", "spac", "deriv", "pref", "cef")


def _old_rank(rows_or_df):
    """Oracle: the pre-change function body, verbatim."""
    if hasattr(rows_or_df, 'empty'):
        df = rows_or_df
        if df.empty:
            return set(), set()
        elig = df[
            df.get('tradable', False).fillna(False)
            & (df.get('status', '') == 'active')
            & df['market_cap'].notna()
        ].copy()
        if elig.empty:
            return set(), set()
        elig = elig.sort_values('market_cap', ascending=False)
        syms = elig['symbol'].astype(str).tolist()
    else:
        ranked = sorted(
            ((r['symbol'], r.get('market_cap')) for r in rows_or_df
             if (r.get('tradable', False) and r.get('status') == 'active'
                 and r.get('market_cap') is not None)),
            key=lambda x: -(x[1] or 0.0),
        )
        syms = [s for s, _ in ranked]
    return set(syms[:1000]), set(syms[:3000])


def _row(sym, cap, st="MISSING", **kw):
    r = {'symbol': sym, 'tradable': True, 'status': 'active', 'market_cap': cap}
    if st != "MISSING":
        r['security_type'] = st
    r.update(kw)
    return r


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.delenv('OPENCLAW_RANK_COMMON_ONLY', raising=False)


def _shapes(rows):
    yield rows
    yield pd.DataFrame(rows)


def test_constant_is_single_source():
    assert um.NON_COMMON_SECURITY_TYPES is universe_meta.NON_COMMON_SECURITY_TYPES
    assert universe_meta.NON_COMMON_SECURITY_TYPES == frozenset(EXCLUDED)


@pytest.mark.parametrize("t", EXCLUDED)
def test_each_excluded_type_removed_both_shapes(t):
    rows = [_row('KEEP', 1e9, 'stock'), _row('X', 9e12, t)]
    for shape in _shapes(rows):
        r1, r3 = rank_in_r1000_r3000(shape)
        assert r1 == {'KEEP'} and r3 == {'KEEP'}


def test_retained_types_both_shapes():
    rows = [_row('S', 5e9, 'stock'), _row('A', 4e9, 'adr'), _row('N', 3e9, None),
            _row('M', 2e9)]  # M: key absent in dict shape
    rows_nan = [dict(r) for r in rows]
    for shape in list(_shapes(rows)):
        r1, _ = rank_in_r1000_r3000(shape)
        assert r1 == {'S', 'A', 'N', 'M'}
    df = pd.DataFrame(rows_nan)
    df.loc[df.symbol == 'M', 'security_type'] = float('nan')
    assert rank_in_r1000_r3000(df)[0] == {'S', 'A', 'N', 'M'}


def test_no_security_type_is_identical_to_old():
    rows = [_row(f"T{i}", float(10_000 - i)) for i in range(3500)]
    rows += [_row('NC', 5e9, tradable=False), _row('NONE', None)]
    for shape in _shapes(rows):
        assert rank_in_r1000_r3000(shape) == _old_rank(shape)
    assert rank_in_r1000_r3000([]) == (set(), set())
    assert rank_in_r1000_r3000(pd.DataFrame()) == (set(), set())


def test_kill_switch_restores_old_pool(monkeypatch):
    rows = [_row('S', 1e9, 'stock'), _row('E', 9e12, 'etf')]
    monkeypatch.setenv('OPENCLAW_RANK_COMMON_ONLY', '0')
    for shape in _shapes(rows):
        assert rank_in_r1000_r3000(shape) == _old_rank(shape) == ({'S', 'E'}, {'S', 'E'})
    monkeypatch.setenv('OPENCLAW_RANK_COMMON_ONLY', '1')
    assert rank_in_r1000_r3000(rows)[0] == {'S'}
    monkeypatch.setenv('OPENCLAW_RANK_COMMON_ONLY', 'yes')
    assert rank_in_r1000_r3000(rows)[0] == {'S'}


def test_displacement_at_the_cutoff():
    rows = [_row(f"ETF{i}", 1e13 - i, 'etf') for i in range(50)]
    rows += [_row(f"S{i}", 1e9 - i, 'stock') for i in range(1000)]
    for shape in _shapes(rows):
        r1, r3 = rank_in_r1000_r3000(shape)
        assert r1 == {f"S{i}" for i in range(1000)} == r3
    assert len(_old_rank(rows)[0]) == 1000 and 'S999' not in _old_rank(rows)[0]


def _alpaca(sym):
    return {"symbol": sym, "asset_class": "us_equity", "exchange": "NYSE", "status": "active",
            "tradable": True, "shortable": True, "fractionable": True, "easy_to_borrow": True,
            "first_seen_at": "2000-01-01", "last_seen_at": "2026-10-01"}


def test_writer_end_to_end_etf_does_not_take_slot():
    syms = [f"S{i}" for i in range(1000)] + ["BIGETF"]
    prof = {s: {"isEtf": False, "isFund": False, "isAdr": False, "mktCap": 1e9 - i}
            for i, s in enumerate(syms[:1000])}
    prof["BIGETF"] = {"isEtf": True, "isFund": False, "isAdr": False, "mktCap": 9e12}
    rows = w.build_metadata_rows(date(2026, 10, 5), [_alpaca(s) for s in syms], prof,
                                 {}, {}, source_tag="t")
    by = {r["symbol"]: r for r in rows}
    assert not by["BIGETF"]["in_r1000"] and not by["BIGETF"]["in_r3000"]
    assert by["S999"]["in_r1000"] and by["S999"]["in_r3000"]
