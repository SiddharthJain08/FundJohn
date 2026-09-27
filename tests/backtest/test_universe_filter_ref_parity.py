"""Sparse-CCA liquidity gate (2026-09-25-sparse-cca-zero-signals, task 2) —
universe-layer parity test.

Reviewer ruling (progress.md, 2026-09-25): the liquidity gate belongs in the
UNIVERSE layer, applied IDENTICALLY in the live engine and the backtest.
S_sparse_cca_mean_revert's manifest already carries
metadata.universe_filter_ref = 'src.strategies.universe_default:tier_liquid'.
This test drives BOTH resolution paths off that ONE shared manifest field,
on a synthetic ticker_metadata_snapshots-shaped table (no DB, no master
parquet, no live engine/backtest run):

  * LIVE:     execution.live_universe.build_strategy_universes
              -> strategies.universe_resolver.UniverseResolver (tier_liquid
                 predicate) -- the function named in the task brief. No code
                 change was needed here: this function already reads
                 metadata.universe_filter_ref generically for ANY strategy
                 (see _manifest_universe_refs/_predicate_name); this test
                 pins that existing behavior for this specific strategy.
  * BACKTEST: backtest.unified_backtest._bounded_resolver's NEW
              universe_filter_ref fallback (OPENCLAW_BT_UNIVERSE_FILTER_REF=1)
              -> backtest.precomputed_resolver.PrecomputedResolver, reading a
              frozen tier_liquid membership snapshot built (in this test) by
              applying the SAME tier_liquid predicate to the SAME synthetic
              metadata -- confirmed (by reading scripts/build_tier_membership.
              py:41-52) to mirror how the real offline artifact is built:
              tiers_for_rows() applies each ladder predicate directly to
              TickerMetadata rows after a coverage-floor check, no
              intermediate "cap" step (that's a live-resolver-only concept,
              a no-op here since the predicate itself already IS tier_liquid).
              This test omits the coverage floor deliberately -- it's
              orthogonal to the predicate-parity question and has no
              backtest-side analog either.

Honesty note (do not oversell this as "engine == backtest on everything"):
the live engine's mirror-clamp (SP-7 spec D3) passes non-equity / absent-
from-metadata tickers straight through to every strategy regardless of its
predicate; the backtest resolver has no passthrough concept at all -- when a
resolver is active, ONLY its frozen membership list is used. This test
proves parity on the CLAMPABLE-EQUITY set the tier_liquid predicate actually
governs, and separately asserts that engine-only passthrough is the ONE, by-
design divergence -- not an oversight.

Also documents (not exercised further here -- out of scope for this task):
tier_liquid's `liquid_tradable` leg has no market-cap/ADV floor, so a
tradable+active+ETB+fractionable micro-cap (e.g. this test's DDD) qualifies
for tier_liquid on both sides identically. Wiring the SAME predicate
consistently does not by itself guarantee tier_liquid excludes every
thinly-traded name a reviewer might call "illiquid" -- see the task-2 report.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
import pytest

from strategies.universe_meta import TickerMetadata
from strategies.universe_resolver import UniverseResolver
from strategies.universe_default import tier_liquid
from backtest.unified_backtest import _bounded_resolver

STRATEGY_ID = 'S_sparse_cca_mean_revert'
REF = 'src.strategies.universe_default:tier_liquid'
AS_OF = date(2026, 6, 8)
SNAPSHOT_DATE = date(2026, 1, 1)
FIXED_TODAY = date(2026, 7, 1)


def _meta(symbol, *, in_sp500=False, in_r1000=False, in_r3000=False,
         tradable=True, status='active', easy_to_borrow=True, fractionable=True):
    return TickerMetadata(
        symbol=symbol, asset_class='us_equity', exchange='NASDAQ',
        status=status, tradable=tradable, shortable=True,
        fractionable=fractionable, easy_to_borrow=easy_to_borrow,
        market_cap=None, adv_usd_20d=None, sector=None, industry=None,
        options_eligible=False, in_sp500=in_sp500, in_r1000=in_r1000,
        in_r3000=in_r3000, listed_date=date(2010, 1, 1), delisted_date=None,
    )


# Six clampable-equity legs exercising every branch of
# tier_liquid = tier_r3000(sp500 | r1000 | r3000) OR liquid_tradable:
SYNTHETIC_METADATA = {
    'AAA': _meta('AAA', in_sp500=True),                                    # sp500 leg
    'BBB': _meta('BBB', in_r1000=True),                                    # r1000 leg
    'CCC': _meta('CCC', in_r3000=True),                                    # r3000 leg
    'DDD': _meta('DDD'),                                                   # liquid_tradable leg only (no index membership)
    'EEE': _meta('EEE', easy_to_borrow=False),                             # fails liquid_tradable (not ETB), no index -> excluded
    'FFF': _meta('FFF', tradable=False),                                   # not tradable -> excluded
}
EXPECTED_TIER_LIQUID = sorted(
    sym for sym, meta in SYNTHETIC_METADATA.items() if tier_liquid(meta, AS_OF))
# = ['AAA', 'BBB', 'CCC', 'DDD']

# Passthrough-only names (SP-7 mirror-clamp, spec D3): never governed by the
# predicate at all. QQQ_ETF is present in ticker metadata but category='etf'
# (not 'equity'); ABSENT1 is not in metadata/universe_config at all (e.g. a
# newly-listed or delisted name). Both should survive on the ENGINE side
# unconditionally and have no analog on the backtest side.
PASSTHROUGH = ['QQQ_ETF', 'ABSENT1']
FALLBACK_UNIVERSE = list(SYNTHETIC_METADATA) + PASSTHROUGH


class FakeDB:
    """Mirrors tests/strategies/test_universe_resolver.py's FakeDB."""
    def __init__(self, rows):
        self.rows = rows

    def fetch_metadata_as_of(self, as_of):
        return list(self.rows)


class FakeCoverage:
    """Coverage floor always passes -- orthogonal to the tier_liquid
    predicate this test is about; the backtest side has no floor concept."""
    def has_floor(self, symbol, as_of):
        return True


@pytest.fixture
def manifest_path(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {
        STRATEGY_ID: {'state': 'live', 'metadata': {'universe_filter_ref': REF}},
    }}))
    return p


@pytest.fixture
def artifact_dir(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    pd.DataFrame([
        {'run_id': 'shrink-t', 'tier': 'tier_liquid',
         'snapshot_date': SNAPSHOT_DATE.isoformat(), 'symbols': EXPECTED_TIER_LIQUID},
    ]).to_parquet(d / 'universe_tier_membership_shrink-20260101.parquet', index=False)
    return d


def _engine_universe(manifest_path, monkeypatch):
    """execution.live_universe.build_strategy_universes -- the function
    named in the task brief -- driven by a REAL UniverseResolver over the
    same synthetic metadata, with the SAME manifest file the backtest side
    reads below. monkeypatch.setattr (not a raw mutation) so MANIFEST_PATH
    is restored automatically at the end of the calling test, never leaking
    into a later test that imports the same module object."""
    import execution.live_universe as lu
    monkeypatch.setattr(lu, 'MANIFEST_PATH', str(manifest_path))

    rows = [_Row(sym, meta) for sym, meta in SYNTHETIC_METADATA.items()]
    resolver = UniverseResolver(
        db=FakeDB(rows), coverage=FakeCoverage(),
        manifest_loader=lambda: json.loads(manifest_path.read_text()),
        today_fn=lambda: FIXED_TODAY,
    )
    # meta_fetch/category_fetch drive ONLY the mirror-clamp's
    # is_clampable_equity check (live_universe.py:113-119) -- every
    # tier_liquid-governed name is a plain us_equity/equity row here;
    # QQQ_ETF/ABSENT1 are deliberately excluded so they fall through as
    # non-clampable passthrough.
    meta_fetch = lambda: {s: ('us_equity', m.in_sp500) for s, m in SYNTHETIC_METADATA.items()}
    category_fetch = lambda: {s: 'equity' for s in SYNTHETIC_METADATA}
    out = lu.build_strategy_universes(
        [STRATEGY_ID], AS_OF, FALLBACK_UNIVERSE, resolver=resolver,
        meta_fetch=meta_fetch, category_fetch=category_fetch)
    return out[STRATEGY_ID]


class _Row:
    def __init__(self, symbol, metadata):
        self.symbol = symbol
        self.metadata = metadata


def _backtest_universe(manifest_path, artifact_dir, monkeypatch):
    """backtest.unified_backtest._bounded_resolver -- the static-universe
    seam named in the task brief -- with the new fallback opted in."""
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    resolver = _bounded_resolver(STRATEGY_ID, manifest_path=manifest_path,
                                 data_dir=artifact_dir)
    assert resolver is not None, 'expected the ref fallback to bound a resolver'
    return sorted(resolver.resolve(STRATEGY_ID, AS_OF))


def test_engine_resolves_tier_liquid_predicate(manifest_path, monkeypatch):
    """No code change needed on the engine side -- pins the existing,
    already-generic behavior for this specific strategy's manifest entry."""
    info = _engine_universe(manifest_path, monkeypatch)
    assert info['predicate'] == 'tier_liquid'
    assert info['adopted'] is True
    assert info['error'] is None
    assert set(EXPECTED_TIER_LIQUID) <= set(info['universe'])
    assert 'EEE' not in info['universe']
    assert 'FFF' not in info['universe']


def test_backtest_resolves_same_tier_via_fallback(manifest_path, artifact_dir, monkeypatch):
    result = _backtest_universe(manifest_path, artifact_dir, monkeypatch)
    assert result == EXPECTED_TIER_LIQUID


def test_engine_and_backtest_agree_on_the_clampable_equity_set(
        manifest_path, artifact_dir, monkeypatch):
    """THE parity proof: on the set of names the tier_liquid predicate
    actually governs (the six clampable equities), the live engine and the
    backtest produce the IDENTICAL ticker set for S_sparse_cca_mean_revert."""
    engine_universe = set(_engine_universe(manifest_path, monkeypatch)['universe'])
    backtest_universe = set(_backtest_universe(manifest_path, artifact_dir, monkeypatch))

    governed = set(SYNTHETIC_METADATA)  # AAA..FFF
    assert engine_universe & governed == backtest_universe == set(EXPECTED_TIER_LIQUID)


def test_passthrough_is_the_one_documented_divergence(
        manifest_path, artifact_dir, monkeypatch):
    """Honesty check (not swept under the rug): the engine's mirror-clamp
    passthrough (non-equity / absent-from-metadata tickers) has no backtest
    analog when a resolver is active. The ONLY difference between the two
    sides' output is exactly the passthrough set -- nothing else leaks."""
    engine_universe = set(_engine_universe(manifest_path, monkeypatch)['universe'])
    backtest_universe = set(_backtest_universe(manifest_path, artifact_dir, monkeypatch))

    assert engine_universe - backtest_universe == set(PASSTHROUGH)
    assert backtest_universe - engine_universe == set()  # backtest never has extra names
