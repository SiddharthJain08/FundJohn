"""Amendment 1 D-C2: manifest metadata.backtest_tickers -> load_prices_panels(tickers=)."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from strategies.base import BaseStrategy, CANONICAL_REGIMES  # noqa: E402


def _manifest(tmp_path, entry):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'S_x': entry}}))
    return p


def test_reads_sorted_unique_symbols(tmp_path):
    p = _manifest(tmp_path, {'state': 'live', 'metadata': {'backtest_tickers': ['SPY', 'QQQ', 'SPY', '']}})
    assert ub._manifest_backtest_tickers('S_x', manifest_path=p) == ['QQQ', 'SPY']


def test_absent_or_invalid_is_none(tmp_path, capsys):
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': []}})) is None
    capsys.readouterr()  # discard output so far; isolate the string-case warning below
    # Minor #3 (2026-08-29 final fix wave): present-but-malformed (a bare
    # string, not a list) must log a warning rather than fail silently into
    # a full-panel read.
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': 'SPY'}})) is None
    out = capsys.readouterr().out
    assert 'S_x' in out and 'SPY' in out and 'WARNING' in out
    assert ub._manifest_backtest_tickers('S_other', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=tmp_path / 'missing.json') is None


# ── Important #3: restrict_universe_to_panel intersection ────────────────────

class _FixedUniverseResolver:
    """Resolver stand-in: returns a fixed universe regardless of as_of, like
    a static/coupling override — as opposed to _bounded_resolver's per-bar
    point-in-time resolution."""

    def __init__(self, universe):
        self._universe = list(universe)

    def resolve(self, strategy_id, as_of=None):
        return list(self._universe)


class _UniverseRecorder(BaseStrategy):
    """Stand-in strategy: records the universe it is handed each bar, emits
    no signals."""
    id = 'stub_universe_recorder'
    min_lookback = 1
    active_in_regimes = list(CANONICAL_REGIMES)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen: list = []

    def generate_signals(self, prices, regime, universe, aux_data=None):
        self.seen.append(list(universe))
        return []


def _tiny_panel(n=8):
    """2-ticker panel (AAA, BBB); n bars is enough to clear
    _per_bar_simulate's min_lookback(=1)+5 warm-up with a couple of days to
    spare — kept small so the test runs in well under a second."""
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close_wide = pd.DataFrame({'AAA': [100.0 + i for i in range(n)],
                               'BBB': [50.0 + i for i in range(n)]}, index=dates)
    close_wide.index.name = 'date'
    bars_by_ticker = {}
    for t, base in (('AAA', 100.0), ('BBB', 50.0)):
        closes = [base + i for i in range(n)]
        bars_by_ticker[t] = pd.DataFrame(
            {'open': closes, 'high': [c + 0.5 for c in closes],
             'low': [c - 0.5 for c in closes], 'close': closes},
            index=dates)
    regimes = pd.Series({d: 'LOW_VOL' for d in dates})
    return close_wide, bars_by_ticker, regimes, dates


def test_restrict_universe_to_panel_intersects():
    """Static universe of 3 (CCC absent from the 2-ticker panel): the default
    hands generate_signals all 3; restrict_universe_to_panel=True narrows it
    to the 2 panel tickers, order preserved."""
    close_wide, bars_by_ticker, regimes, dates = _tiny_panel()
    resolver_universe = ['CCC', 'BBB', 'AAA']  # CCC first, absent from the panel
    fake_aux = lambda date, **kw: {'options': {}}  # skip the real (DB-backed) aux loader

    with patch('strategies.aux_data_loader.load_aux_data', side_effect=fake_aux):
        recorder_default = _UniverseRecorder()
        out_default = ub._per_bar_simulate(
            recorder_default, close_wide, bars_by_ticker, regimes, dates[0], dates[-1],
            strategy_id='stub_universe_recorder',
            resolver=_FixedUniverseResolver(resolver_universe))

        recorder_restricted = _UniverseRecorder()
        out_restricted = ub._per_bar_simulate(
            recorder_restricted, close_wide, bars_by_ticker, regimes, dates[0], dates[-1],
            strategy_id='stub_universe_recorder',
            resolver=_FixedUniverseResolver(resolver_universe),
            restrict_universe_to_panel=True)

    assert recorder_default.seen
    assert all(u == ['CCC', 'BBB', 'AAA'] for u in recorder_default.seen)
    assert out_default['universe_sizes'] and all(s == 3 for s in out_default['universe_sizes'])

    assert recorder_restricted.seen
    assert all(u == ['BBB', 'AAA'] for u in recorder_restricted.seen)  # CCC dropped, order preserved
    assert out_restricted['universe_sizes'] and all(s == 2 for s in out_restricted['universe_sizes'])
