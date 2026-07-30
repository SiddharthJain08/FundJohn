"""Acting-set ingest plan resolver (2026-07-29 three-tier ingestion).

The 15:00 ET tier-1 ingest and the regime-change delta ingest scope their
fetches from this resolver; it must mirror the engine's own selection
(approved ∩ regime-eligible, SP-7 universes) and never silently shrink.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import acting_ingest_plan as aip  # noqa: E402


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        assert "status = 'approved'" in sql

    def fetchall(self):
        return [(r,) for r in self._rows]


def _wire(monkeypatch, *, approved, eligible, universes, requirements):
    monkeypatch.setattr(
        'strategies.regime_gate.is_eligible',
        lambda sid, regime: sid in eligible)
    monkeypatch.setattr(
        'execution.live_universe.build_strategy_universes',
        lambda sids, as_of, fb, **kw: {
            sid: {'universe': universes.get(sid, fb), 'error': None}
            for sid in sids})
    monkeypatch.setattr(aip, 'load_requirements',
                        lambda sid: requirements[sid])
    return _FakeCur(approved)


class TestResolveActingSet:
    def test_approved_intersect_eligible(self, monkeypatch):
        cur = _wire(monkeypatch,
                    approved=['A', 'B', 'C'], eligible={'A', 'C'},
                    universes={}, requirements={})
        assert aip.resolve_acting_set(cur, 'LOW_VOL') == ['A', 'C']


class TestResolveIngestPlan:
    def test_categories_scoped_to_consumer_universes(self, monkeypatch):
        cur = _wire(
            monkeypatch,
            approved=['S_opts', 'S_px'], eligible={'S_opts', 'S_px'},
            universes={'S_opts': ['AAPL', 'MSFT'], 'S_px': ['SPY', 'QQQ']},
            requirements={
                'S_opts': {'required': ['prices', 'options_eod'], 'optional': []},
                'S_px':   {'required': ['prices'], 'optional': ['macro']},
            })
        plan = aip.resolve_ingest_plan(cur, 'LOW_VOL', '2026-07-29',
                                       fallback_universe=['FB'])
        assert plan['acting'] == ['S_opts', 'S_px']
        # prices = union of BOTH universes; options only the options consumer's.
        assert plan['categories']['prices'] == ['AAPL', 'MSFT', 'QQQ', 'SPY']
        assert plan['categories']['options_eod'] == ['AAPL', 'MSFT']
        # optional macro counts, and is marketwide (no ticker scoping).
        assert plan['marketwide'] == ['macro']
        assert plan['consumers']['options_eod'] == ['S_opts']

    def test_resolver_failure_fails_open_to_fallback(self, monkeypatch):
        cur = _wire(
            monkeypatch,
            approved=['S1'], eligible={'S1'}, universes={},
            requirements={'S1': {'required': ['prices'], 'optional': []}})
        monkeypatch.setattr(
            'execution.live_universe.build_strategy_universes',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
        plan = aip.resolve_ingest_plan(cur, 'LOW_VOL', '2026-07-29',
                                       fallback_universe=['SPY', 'IWM'])
        assert plan['categories']['prices'] == ['IWM', 'SPY']

    def test_unknown_category_surfaces_not_dropped(self, monkeypatch):
        cur = _wire(
            monkeypatch,
            approved=['S1'], eligible={'S1'},
            universes={'S1': ['TSLA']},
            requirements={'S1': {'required': ['prices', 'lidar_pointclouds'],
                                 'optional': []}})
        plan = aip.resolve_ingest_plan(cur, 'HIGH_VOL', '2026-07-29')
        assert plan['categories']['lidar_pointclouds'] == ['TSLA']

    def test_derived_categories_not_fetched(self, monkeypatch):
        cur = _wire(
            monkeypatch,
            approved=['S1'], eligible={'S1'},
            universes={'S1': ['SPY']},
            requirements={'S1': {'required': ['prices'],
                                 'optional': ['realized_vol']}})
        plan = aip.resolve_ingest_plan(cur, 'LOW_VOL', '2026-07-29')
        assert 'realized_vol' not in plan['categories']

    def test_empty_acting_set(self, monkeypatch):
        cur = _wire(monkeypatch, approved=['A'], eligible=set(),
                    universes={}, requirements={})
        plan = aip.resolve_ingest_plan(cur, 'CRISIS', '2026-07-29')
        assert plan == {'regime_state': 'CRISIS', 'acting': [],
                        'categories': {}, 'marketwide': [], 'consumers': {}}


class TestPlanDelta:
    _NEW = {'regime_state': 'HIGH_VOL', 'acting': ['S1', 'S2'],
            'categories': {'prices': ['AAPL', 'SPY'], 'insider': ['AAPL']},
            'marketwide': ['macro'], 'consumers': {'insider': ['S2']}}

    def test_none_fresh_returns_whole_plan(self):
        assert aip.plan_delta(self._NEW, None) is self._NEW

    def test_covered_tickers_and_categories_drop_out(self):
        fresh = {'categories': {'prices': ['SPY'], 'insider': ['AAPL']},
                 'marketwide': ['macro']}
        delta = aip.plan_delta(self._NEW, fresh)
        assert delta['categories'] == {'prices': ['AAPL']}
        assert delta['marketwide'] == []

    def test_disjoint_fresh_keeps_everything(self):
        fresh = {'categories': {'prices': ['QQQ']}, 'marketwide': []}
        delta = aip.plan_delta(self._NEW, fresh)
        assert delta['categories'] == {'prices': ['AAPL', 'SPY'],
                                       'insider': ['AAPL']}
        assert delta['marketwide'] == ['macro']


class TestLoadRequirementsReal:
    """Against the real backfilled files — the resolver's substrate."""

    def test_hv20_declares_options(self):
        r = aip.load_requirements('S_HV20_iv_dispersion_reversion')
        assert 'options_eod' in r['required']

    def test_missing_file_fails_conservative(self):
        r = aip.load_requirements('S_does_not_exist_xyz')
        assert r == {'required': ['prices'], 'optional': []}


class TestSchemaContract:
    """Both regressions from 2026-07-30, when the plan had never once resolved
    a non-empty scope in production and every caller swallowed it."""

    def test_selects_the_column_the_registry_actually_has(self, monkeypatch):
        """`strategy_registry` keys on `id`; there is no `strategy_id` column.
        The mismatch raised inside every caller's non-blocking except branch,
        so the redeploy preflight logged 'failed' instead of a plan."""
        seen = {}

        class _Cur:
            def execute(self, sql, params=None):
                seen['sql'] = sql

            def fetchall(self):
                return [('A',)]

        monkeypatch.setattr('strategies.regime_gate.is_eligible',
                            lambda sid, regime: True)
        aip.resolve_acting_set(_Cur(), 'LOW_VOL')
        assert 'strategy_id' not in seen['sql']
        assert 'SELECT id FROM strategy_registry' in seen['sql']

    def test_empty_base_universe_raises_rather_than_planning_nothing(self, monkeypatch):
        """build_strategy_universes INTERSECTS its predicate with the base set,
        so an empty base yields zero tickers per strategy with error=None and
        adopted=True — a plan that looks successful and ingests nothing."""
        cur = _wire(monkeypatch, approved=['A'], eligible={'A'},
                    universes={}, requirements={'A': {'required': ['prices'],
                                                     'optional': []}})
        monkeypatch.setattr(aip, 'base_universe', lambda *a, **k: [])
        with pytest.raises(aip.IngestPlanError):
            aip.resolve_ingest_plan(cur, 'LOW_VOL', '2026-07-30')

    def test_base_universe_ignores_the_registry_universe_column(self, monkeypatch):
        """That column holds symbolic labels ('SP500', 'FixedETFlist:SPY,...'),
        not tickers — which is why the engine derives its base from the master
        price panel instead."""
        import pyarrow as pa
        monkeypatch.setattr(
            'pyarrow.parquet.read_table',
            lambda *a, **k: pa.table({'ticker': ['AAPL', 'MSFT', 'AAPL']}))
        assert aip.base_universe() == ['AAPL', 'MSFT']

    def test_unresolvable_strategy_widens_to_the_base_not_to_nothing(self, monkeypatch):
        """C1 fail-open: a strategy whose predicate resolves empty ingests the
        whole base universe. Ingest scope may over-fetch; it may never
        silently shrink below what the engine will read."""
        cur = _wire(monkeypatch, approved=['A'], eligible={'A'},
                    universes={'A': []},
                    requirements={'A': {'required': ['options_eod'], 'optional': []}})
        plan = aip.resolve_ingest_plan(cur, 'LOW_VOL', '2026-07-30',
                                       fallback_universe=['AAPL', 'MSFT'])
        assert plan['categories']['options_eod'] == ['AAPL', 'MSFT']
