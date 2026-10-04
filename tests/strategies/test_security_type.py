"""Universe security type, Phase 1 — mapping, TickerMetadata, predicates,
resolver parity, enumerations, DB-adapter overlay, migration 163. Fakes only."""
from __future__ import annotations

import itertools
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.strategies import universe_default as ud
from src.strategies.universe_meta import (
    TickerMetadata, security_type_from_profile, security_types_from_profiles,
    overlay_security_types)
from src.strategies.universe_resolver import UniverseResolver

AS_OF = date(2026, 1, 1)
OLD_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
NEW_TIERS = ('stocks_sp500', 'stocks_r1000', 'stocks_r3000', 'stocks_liquid')
BASE = dict(symbol='T', asset_class='us_equity', exchange='NASDAQ', status='active',
            tradable=True, shortable=True, fractionable=True, easy_to_borrow=True,
            market_cap=None, adv_usd_20d=None, sector=None, industry=None,
            options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
            listed_date=None, delisted_date=None)


def _meta(**over):
    return TickerMetadata(**{**BASE, **over})


# ── mapping ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('profile,expected', [
    ({'isEtf': True, 'isFund': True, 'isAdr': True}, 'etf'),
    ({'isEtf': False, 'isFund': True, 'isAdr': True}, 'fund'),
    ({'isEtf': False, 'isFund': False, 'isAdr': True}, 'adr'),
    ({'isEtf': False, 'isFund': False, 'isAdr': False}, 'stock'),
    ({'isEtf': None, 'isFund': None, 'isAdr': None}, 'stock'),
    ({'_fetched_at': 'x', '_empty': True}, None),
    ({}, None), (None, None), ('junk', None),
    ({'sector': 'Tech', 'mktCap': 1.0}, None),   # legacy entry, no flags -> unknown
    ({'isEtf': False, 'isFund': False, 'isAdr': True, 'industry': 'Shell Companies'}, 'spac'),
    ({'isEtf': True, 'industry': 'Shell Companies'}, 'etf'),
])
def test_security_type_from_profile(profile, expected):
    assert security_type_from_profile(profile) == expected


def test_security_types_from_profiles_drops_unknown():
    out = security_types_from_profiles({
        'SPY': {'isEtf': True, 'isFund': False, 'isAdr': False},
        'AAPL': {'isEtf': False, 'isFund': False, 'isAdr': False},
        'BRK.B': {'_empty': True}})
    assert out == {'SPY': 'etf', 'AAPL': 'stock'}


# ── TickerMetadata ───────────────────────────────────────────────────────────
def test_from_row_with_and_without_security_type():
    row = dict(BASE)
    assert TickerMetadata.from_row(row).security_type is None
    assert TickerMetadata.from_row({**row, 'security_type': 'etf'}).security_type == 'etf'


def test_from_row_other_fields_stay_strict():
    row = dict(BASE); row.pop('market_cap')
    with pytest.raises(KeyError):
        TickerMetadata.from_row(row)


def test_security_type_is_last_field_with_default():
    assert list(TickerMetadata.__dataclass_fields__)[-1] == 'security_type'
    assert _meta().security_type is None


# ── predicates ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('st,sp,expected', [
    ('stock', False, True), ('adr', False, True), ('stock', True, True),
    ('etf', False, False), ('fund', False, False),
    ('etf', True, False), ('fund', True, False),      # a typed fund is never rescued by in_sp500
    (None, True, True),                                # BRK.B: unknown but index member
    (None, False, False),                              # unknown preferred share
])
def test_common_stock_truth_table(st, sp, expected):
    assert ud.common_stock(_meta(security_type=st, in_sp500=sp), AS_OF) is expected


def _grid():
    for st, sp, r1, r3, tr, etb, fr, act in itertools.product(
            (None, 'stock', 'adr', 'etf', 'fund'), (0, 1), (0, 1), (0, 1),
            (0, 1), (0, 1), (0, 1), (0, 1)):
        yield _meta(security_type=st, in_sp500=bool(sp), in_r1000=bool(r1),
                    in_r3000=bool(r3), tradable=bool(tr), easy_to_borrow=bool(etb),
                    fractionable=bool(fr), status='active' if act else 'inactive')


def test_stock_tiers_subset_of_base_and_exclude_funds():
    for m in _grid():
        for new, old in ud.STOCK_TIER_BASE.items():
            if getattr(ud, new)(m, AS_OF):
                assert getattr(ud, old)(m, AS_OF), (new, m)
                assert m.security_type not in ('etf', 'fund')
            # definition: existing tier AND common_stock
            assert getattr(ud, new)(m, AS_OF) == (
                getattr(ud, old)(m, AS_OF) and ud.common_stock(m, AS_OF))


def test_existing_predicates_ignore_security_type():
    for m in _grid():
        from dataclasses import replace
        for other in ('etf', 'stock', None):
            m2 = replace(m, security_type=other)
            for name, fn in ud.CANDIDATE_PREDICATES.items():
                if name.startswith('stocks_'):
                    continue
                assert fn(m, AS_OF) == fn(m2, AS_OF), name


# ── enumerations ─────────────────────────────────────────────────────────────
def test_new_names_registered_everywhere():
    for n in NEW_TIERS:
        assert n in ud.CANDIDATE_PREDICATES          # adoption / set_universe / grid / mint-validator
        assert n in ud.LADDER_TIER_PREDICATES        # excluded from the PaperHunter mint menu
        assert callable(getattr(ud, n))
        assert ud.CANDIDATE_PREDICATES[n] is getattr(ud, n)
    mint_menu = [n for n in ud.CANDIDATE_PREDICATES if n not in ud.LADDER_TIER_PREDICATES]
    assert not [n for n in mint_menu if n.startswith('stocks_')]
    assert len(mint_menu) == 12


def test_new_names_resolve_as_universe_filter_ref_and_pass_sandbox():
    from src.strategies.lifecycle import _sandbox_check_predicate
    for n in NEW_TIERS:
        _sandbox_check_predicate(f'src.strategies.universe_default:{n}')


def test_new_names_accepted_as_backtest_cap(tmp_path):
    """_bounded_resolver / PrecomputedResolver accept any tier present in the artifact."""
    import pandas as pd
    from backtest.precomputed_resolver import PrecomputedResolver
    df = pd.DataFrame([{'run_id': 'r', 'tier': t, 'snapshot_date': '2025-12-31',
                        'symbols': ['AAPL']} for t in NEW_TIERS + OLD_TIERS])
    p = tmp_path / 'universe_tier_membership_x.parquet'
    df.to_parquet(p, index=False)
    for t in NEW_TIERS:
        assert PrecomputedResolver(p, t).resolve('S', AS_OF) == ['AAPL']


def test_ladder_consumers_do_not_see_stock_tiers():
    from backtest.universe_ladder_selection import LADDER_TIERS as L1
    from scripts.build_tier_membership import LADDER_TIERS as L2, STOCK_TIERS, ALL_TIERS
    assert L1 == L2 == OLD_TIERS
    assert STOCK_TIERS == NEW_TIERS and ALL_TIERS == OLD_TIERS + NEW_TIERS


# ── resolver parity ──────────────────────────────────────────────────────────
class _Row:
    def __init__(self, m): self.metadata = m; self.symbol = m.symbol


class _DB:
    def __init__(self, metas): self._m = metas
    def fetch_metadata_as_of(self, as_of): return [_Row(m) for m in self._m]


class _Cov:
    def has_floor(self, s, d): return True


def _universe(populated: bool):
    metas = []
    for i, m in enumerate(_grid()):
        from dataclasses import replace
        metas.append(replace(m, symbol=f'S{i:05d}',
                             security_type=(['etf', 'fund', 'adr', 'stock', None][i % 5]
                                            if populated else None)))
    return metas


def test_resolver_parity_existing_tiers_identical_with_and_without_type():
    manifest = {'strategies': {t: {'metadata': {
        'universe_filter_ref': f'src.strategies.universe_default:{t}'}} for t in OLD_TIERS}}
    out = {}
    for populated in (False, True):
        r = UniverseResolver(_DB(_universe(populated)), _Cov(),
                             manifest_loader=lambda: manifest, today_fn=lambda: AS_OF)
        out[populated] = {t: r.resolve(t, AS_OF) for t in OLD_TIERS}
    assert out[False] == out[True]
    assert all(out[False][t] for t in OLD_TIERS)


def test_resolver_stock_tier_is_subset_when_populated():
    manifest = {'strategies': {n: {'metadata': {
        'universe_filter_ref': f'src.strategies.universe_default:{n}'}}
        for n in OLD_TIERS + NEW_TIERS}}
    metas = _universe(True)
    r = UniverseResolver(_DB(metas), _Cov(), manifest_loader=lambda: manifest,
                         today_fn=lambda: AS_OF)
    typed = {m.symbol: m.security_type for m in metas}
    for new, old in ud.STOCK_TIER_BASE.items():
        got = set(r.resolve(new, AS_OF))
        assert got <= set(r.resolve(old, AS_OF))
        assert not [s for s in got if typed[s] in ('etf', 'fund')]


# ── overlay / DB adapter ─────────────────────────────────────────────────────
def test_overlay_applies_latest_type_to_every_row_and_keeps_unlisted():
    rows = [_Row(_meta(symbol='A')), _Row(_meta(symbol='B', security_type='adr')),
            _Row(_meta(symbol='C'))]
    out = overlay_security_types(rows, {'A': 'etf'})
    assert [r.metadata.security_type for r in out] == ['etf', 'adr', None]


class _FakeCur:
    def __init__(self, has_col, types): self.has_col, self.types, self.last = has_col, types, ''
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None): self.last = sql
    def fetchone(self): return (1,) if self.has_col else None
    def fetchall(self): return list(self.types.items())


class _FakeConn:
    def __init__(self, has_col=True, types=None): self.c = _FakeCur(has_col, types or {})
    def cursor(self): return self.c


def test_db_adapter_overlay_and_missing_column_fallback(monkeypatch):
    from src.strategies import _db_adapters as dba
    db = dba.PostgresMetadataDB('dsn', conn=_FakeConn(True, {'SPY': 'etf'}))
    assert db.fetch_latest_security_types() == {'SPY': 'etf'}
    assert 'security_type IS NOT NULL' in dba.LATEST_SECURITY_TYPE_SQL
    assert 'ORDER BY symbol, snapshot_date DESC' in dba.LATEST_SECURITY_TYPE_SQL
    db2 = dba.PostgresMetadataDB('dsn', conn=_FakeConn(False))
    assert db2.fetch_latest_security_types() == {}      # pre-163 DB tolerated

    monkeypatch.setattr(dba.PostgresMetadataDB, '_fetch',
                        lambda self, c, as_of: [_Row(_meta(symbol='SPY')), _Row(_meta(symbol='X'))])
    rows = db.fetch_metadata_as_of(AS_OF)
    assert [r.metadata.security_type for r in rows] == ['etf', None]


# ── migration 163 ────────────────────────────────────────────────────────────
def test_migration_163_strictly_additive():
    sql = (ROOT / 'src/database/migrations/163_ticker_metadata_security_type.sql').read_text()
    body = '\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--'))
    assert not re.search(r'\b(DROP|DELETE|TRUNCATE|RENAME|UPDATE)\b', body, re.I)
    assert re.search(r'ADD COLUMN IF NOT EXISTS security_type TEXT', body)
    assert 'COMMENT ON COLUMN ticker_metadata_snapshots.security_type' in body


def test_overlay_failure_is_fail_open_and_memoized(monkeypatch):
    """A failing LATEST_SECURITY_TYPE_SQL (after the column probe passed) must not
    break the existing metadata read: rows load with security_type None, the
    connection is rolled back, and the failing query is not re-issued."""
    from src.strategies import _db_adapters as dba

    class Cur(_FakeCur):
        def execute(self, sql, params=None):
            self.executed.append(sql)
            if sql is dba.LATEST_SECURITY_TYPE_SQL:
                raise RuntimeError('statement timeout')

    class Conn:
        def __init__(self): self.c = Cur(True, {}); self.c.executed = []; self.rollbacks = 0
        def cursor(self): return self.c
        def rollback(self): self.rollbacks += 1

    conn = Conn()
    monkeypatch.setattr(dba.PostgresMetadataDB, '_fetch',
                        lambda self, c, as_of: [_Row(_meta(symbol='AAPL')), _Row(_meta(symbol='SPY'))])
    db = dba.PostgresMetadataDB('dsn', conn=conn)
    rows = db.fetch_metadata_as_of(AS_OF)
    assert [r.metadata.symbol for r in rows] == ['AAPL', 'SPY']
    assert all(r.metadata.security_type is None for r in rows)
    assert conn.rollbacks == 1
    db.fetch_metadata_as_of(date(2026, 1, 2))          # next as_of
    assert conn.c.executed.count(dba.LATEST_SECURITY_TYPE_SQL) == 1   # not retried


# ── expanded mapping: instruments that are not common equity ─────────────────
def _p(name, industry='Software', adr=False):
    return {'isEtf': False, 'isFund': False, 'isAdr': adr, 'companyName': name, 'industry': industry}


@pytest.mark.parametrize('name,industry,expected', [
    # caught (real names from the vendor cache)
    ('Revolution Medicines, Inc. Warrant', 'Biotechnology', 'deriv'),                      # RVMDW
    ('Duke Energy Corporation Units 1.08.29', 'Regulated Electric', 'deriv'),              # DUKU
    ('Gen Digital Inc. Contingent Value Rights', 'Software - Infrastructure', 'deriv'),
    ('Prudential Financial, Inc. 4.125% Junior Subordinated Notes due 2060', 'Insurance - Life', 'pref'),  # PFH
    ('MicroStrategy Incorporated 10.00% Series A Perpetual Strife Preferred Stock', 'Software - Application', 'pref'),  # STRF
    ('AGNC Investment Corp. 8.75% Series H Fixed-Rate Cumulative Redeemable Preferred Stock', 'REIT - Mortgage', 'pref'),  # AGNCZ
    ('Alphabet Inc. Depository Shs Repr 1/20th Conv Pfd Registered Shs', 'Software - Application', 'pref'),
    ('Churchill Capital Corp XI', 'Shell Companies', 'spac'),
    ('Churchill Capital Corp XI Units', 'Shell Companies', 'spac'),                        # spac outranks deriv
    ('Cohen & Steers REIT and Preferred Income Fund, Inc.', 'Asset Management', 'cef'),    # RNP (not pref)
    ('Blackstone Secured Lending Fund', 'Asset Management', 'cef'),                        # BXSL
    ('MSC Income Fund, Inc.', 'Asset Management', 'cef'),                                  # MSIF
    ('Ares Capital Corporation', 'Asset Management', 'cef'),
    # must stay stock / adr
    ('Apple Inc.', 'Consumer Electronics', 'stock'),
    ('BlackRock, Inc.', 'Asset Management', 'stock'),
    ('T. Rowe Price Group, Inc.', 'Asset Management', 'stock'),
    ('Blue Owl Capital Inc.', 'Asset Management', 'stock'),
    ('Invesco Ltd.', 'Asset Management', 'stock'),
    ('United Rentals, Inc.', 'Rental & Leasing Services', 'stock'),
    ('United Parcel Service, Inc.', 'Integrated Freight & Logistics', 'stock'),
    ('Unity Software Inc.', 'Software - Application', 'stock'),
    ('Rightmove plc', 'Internet Content & Information', 'stock'),
    ('Preferred Bank', 'Banks - Regional', 'stock'),
    ('Senior Housing Properties Trust', 'REIT', 'stock'),
    ('Noteworthy Medical Systems', 'Medical', 'stock'),
    ('Arm Holdings plc American Depositary Shares', 'Semiconductors', 'stock'),
])
def test_expanded_mapping_conservative(name, industry, expected):
    assert security_type_from_profile(_p(name, industry)) == expected


def test_adr_still_adr_and_only_after_other_rules():
    assert security_type_from_profile(_p('Taiwan Semiconductor Manufacturing Company Limited',
                                         'Semiconductors', adr=True)) == 'adr'
    assert security_type_from_profile(_p('Foo SA Warrants', adr=True)) == 'deriv'


def test_common_stock_excludes_new_types():
    for t in ('spac', 'deriv', 'pref', 'cef'):
        assert ud.common_stock(_meta(security_type=t, in_sp500=True), AS_OF) is False


_CACHE = Path('/root/openclaw/data/.cache/fmp_profile.json')


@pytest.mark.skipif(not _CACHE.exists(), reason='vendor profile cache not on this box')
def test_real_cache_names():
    import json
    d = json.loads(_CACHE.read_text())
    caught = {'RVMDW': 'deriv', 'PFH': 'pref', 'STRF': 'pref', 'AGNCZ': 'pref',
              'DUKU': 'deriv', 'RNP': 'cef', 'BXSL': 'cef', 'MSIF': 'cef'}
    for sym, t in caught.items():
        assert security_type_from_profile(d[sym]) == t, sym
    spacs = [k for k, v in d.items() if security_type_from_profile(v) == 'spac']
    assert len(spacs) > 100 and security_type_from_profile(d['CCXI']) == 'spac'
    for sym in ('AAPL', 'BLK', 'TROW', 'URI', 'UPS', 'U', 'PFBC', 'BX', 'KKR'):
        assert security_type_from_profile(d[sym]) == 'stock', sym
    assert security_type_from_profile(d['TSM']) == 'adr'
    assert security_type_from_profile(d['SPY']) == 'etf'
    assert security_type_from_profile(d['BRK.B']) is None
