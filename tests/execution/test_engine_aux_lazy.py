"""M3 (signals-memory task 3, 2026-09-28): lazy aux loading.

engine.py's load_aux_data() loads each aux_data kind (financials,
insider_txns, options, prices_30m, macro, sentiment) only when a strategy
that will RUN this cycle needs it. Default OFF (OPENCLAW_AUX_LAZY unset/'0'
-> byte-identical to pre-M3 behaviour). '1' -> the plan actually gates the
load. 'shadow' -> computes + logs the plan but still loads everything. See
.superpowers/sdd/2026-09-28-signals-memory/task-3-brief.md.

No masters, no Postgres/HTTP: every parquet here is a tiny synthetic fixture
written under tmp_path (engine.ROOT is monkeypatched to it); every function
that would otherwise touch a live master, an intraday overlay, or Postgres
(_load_options_window, _apply_options_surface, _sentiment_slice) is replaced
with a fake that RECORDS its calls rather than doing real I/O.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402
from execution import acting_ingest_plan  # noqa: E402
from backtest import factor_prescreen  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_shadow_log(monkeypatch, tmp_path):
    """Task 4: the aux plan/drift lines are also appended to a shadow log;
    keep every test in this file out of the real logs/ directory."""
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path / '_shadow'))


class _FakeStrat:
    """Duck-types just enough of a strategy instance for the M3 planner:
    .id, .calendar_edge, .exit_hook. Mirrors test_engine_should_run_bypass.py's
    _RegimeGatedStrat / test_engine_regime_gate.py's _FakeStrategy pattern."""

    def __init__(self, sid, calendar_edge=False, exit_hook=False):
        self.id = sid
        self.calendar_edge = calendar_edge
        self.exit_hook = exit_hook


class _RecordingStrat(_FakeStrat):
    """Runs through run_strategies' real eligibility + aux-slicing path and
    RECORDS exactly the aux_data dict it received in generate_signals — so a
    test can compare what an eager vs. a lazy run actually hands a
    strategy (the brief's literal constraint-5 ask), not just a property of
    load_aux_data's internal `_need()` branching in isolation."""

    def __init__(self, sid):
        super().__init__(sid)
        self.received_aux = None

    def generate_signals(self, prices, regime, universe, aux_data):
        self.received_aux = aux_data
        return []


def _write_requirements(impl_dir, strat_id, required, optional=None):
    (impl_dir / f'{strat_id}.requirements.json').write_text(json.dumps({
        'strategy_id': strat_id, 'required': required, 'optional': optional or [],
    }))


# ═════════════════════════════════════════════════════════════
# _strategy_run_decision — the hoisted run predicate (constraint 1)
# ═════════════════════════════════════════════════════════════

def test_run_decision_eligible(monkeypatch):
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    s = _FakeStrat('S1')
    assert engine._strategy_run_decision(s, 'LOW_VOL') == (True, False)


def test_run_decision_ineligible_no_calendar_edge(monkeypatch):
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    s = _FakeStrat('S1', calendar_edge=False)
    assert engine._strategy_run_decision(s, 'LOW_VOL') == (False, False)


def test_run_decision_ineligible_calendar_edge_runs_through(monkeypatch):
    """A calendar-edge strategy in a non-eligible regime still counts as
    running — the window IS the signal (brief test 3)."""
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    s = _FakeStrat('S1', calendar_edge=True)
    assert engine._strategy_run_decision(s, 'TRANSITIONING') == (True, True)


def test_run_decision_falsy_regime_still_calls_is_eligible(monkeypatch):
    """Regression for a bug caught in review before this landed: an earlier
    draft short-circuited on a falsy strat_regime_str BEFORE calling
    is_eligible, which would have silently skipped a calendar-edge equity
    strategy that OLD run_strategies code still ran through (old code called
    is_eligible(id, equity_regime_str) unconditionally for the equity path,
    with no falsy guard; is_eligible itself returns False for anything not
    in ALL_REGIMES, then the calendar_edge check still runs). The fixed
    predicate must pass strat_regime_str straight through, unconditionally."""
    calls = []
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: calls.append(rs) or False)
    s = _FakeStrat('S1', calendar_edge=False)
    assert engine._strategy_run_decision(s, None) == (False, False)
    assert calls == [None]


def test_run_decision_falsy_regime_calendar_edge_still_runs_through(monkeypatch):
    """The equity-path half of the same regression: a calendar-edge
    strategy with an unknown/falsy regime string still runs through, since
    is_eligible(..., None) legitimately returns False (not a short-circuit)
    and the calendar_edge fallback then applies exactly as for any other
    ineligible regime."""
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    s = _FakeStrat('S1', calendar_edge=True)
    assert engine._strategy_run_decision(s, None) == (True, True)


# ═════════════════════════════════════════════════════════════
# _running_strategy_ids — the run SET (constraint 1), incl. exit-hook +
# crypto handling
# ═════════════════════════════════════════════════════════════

def test_running_ids_eligible_and_ineligible(monkeypatch):
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: sid == 'S_on')
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    strategies = [_FakeStrat('S_on'), _FakeStrat('S_off')]
    assert engine._running_strategy_ids(strategies, 'LOW_VOL') == {'S_on'}


def test_running_ids_calendar_edge_in_non_eligible_regime_still_runs(monkeypatch):
    """Brief test 3, at the run-SET level."""
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    strategies = [_FakeStrat('S_edge', calendar_edge=True), _FakeStrat('S_plain')]
    assert engine._running_strategy_ids(strategies, 'CRISIS') == {'S_edge'}


def test_running_ids_exit_hook_counts_regardless_of_eligibility(monkeypatch):
    """Exit hooks / regime_exit and any always-on sleeve (S_beta_spy) count
    as running (brief constraint 1) — update_pnl hands the same aux_data to
    should_exit() for every open position's strategy regardless of today's
    regime eligibility."""
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: True)
    strategies = [_FakeStrat('S_hook', exit_hook=True), _FakeStrat('S_plain')]
    assert engine._running_strategy_ids(strategies, 'LOW_VOL') == {'S_hook'}


def test_running_ids_exit_hook_excluded_when_mechanism_off(monkeypatch):
    """update_pnl itself never calls should_exit unless _exit_hook_enabled()
    — an exit_hook strategy contributes nothing to the run set while that
    gate is off (OPENCLAW_EXIT_HOOK_LIVE unset, or an intraday-redeploy
    fragment)."""
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: False)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    strategies = [_FakeStrat('S_hook', exit_hook=True)]
    assert engine._running_strategy_ids(strategies, 'LOW_VOL') == set()


def test_running_ids_crypto_no_regime_available(monkeypatch):
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'crypto')
    monkeypatch.setattr(engine, 'load_crypto_regime_state', lambda: {'state': None})
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    strategies = [_FakeStrat('S_crypto')]
    assert engine._running_strategy_ids(strategies, 'LOW_VOL') == set()


def test_running_ids_crypto_regime_fetched_once_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'crypto')
    monkeypatch.setattr(engine, 'load_crypto_regime_state',
                        lambda: calls.append(1) or {'state': 'RISK_ON'})
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: rs == 'RISK_ON')
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    strategies = [_FakeStrat('S_crypto1'), _FakeStrat('S_crypto2')]
    assert engine._running_strategy_ids(strategies, 'LOW_VOL') == {'S_crypto1', 'S_crypto2'}
    assert calls == [1]


# ═════════════════════════════════════════════════════════════
# _strategy_aux_needs / _needed_aux_kinds — requirements-driven need
# detection with fail-open (constraint 2)
# ═════════════════════════════════════════════════════════════

def test_needs_from_requirements_file(tmp_path, monkeypatch):
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S1', required=['prices', 'options_eod'], optional=['macro'])
    assert engine._strategy_aux_needs(_FakeStrat('S1')) == {'options', 'macro'}


def test_needs_maps_insider_and_ignores_unmapped_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S2', required=['prices', 'insider'],
                        optional=['vol_indices', 'iv_history'])
    # 'prices' is handled by load_prices, not load_aux_data; vol_indices/
    # iv_history have no live engine.py loader — neither is gated here.
    assert engine._strategy_aux_needs(_FakeStrat('S2')) == {'insider_txns'}


def test_needs_earnings_category_maps_to_nothing(tmp_path, monkeypatch):
    """'earnings' is a real requirements.json category
    (S_reversal_momentum_transition_earnings, s_price_earnings_momentum_drift)
    but load_aux_data never sets aux['earnings'] — see the
    _AUX_LAZY_GATED_KINDS docstring."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S3', required=['prices', 'earnings'])
    assert engine._strategy_aux_needs(_FakeStrat('S3')) == set()


def test_needs_unknown_requirements_module_reads_aux_data_fails_open(tmp_path, monkeypatch):
    """Brief test 4: unknown requirements ⇒ load (every gated kind) when
    the AST scan can only confirm aux_data is read somewhere, not which
    kind."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(factor_prescreen, '_module_reads_aux_data', lambda path: True)
    assert engine._strategy_aux_needs(_FakeStrat('S_no_reqs')) == set(engine._AUX_LAZY_GATED_KINDS)


def test_needs_unknown_requirements_confirmed_no_aux_data(tmp_path, monkeypatch):
    """No requirements.json AND the AST scan confirms the module never reads
    aux_data at all -> a confident empty set, not a fail-open — constraint 2
    only asks to fail open when genuinely UNSURE."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(factor_prescreen, '_module_reads_aux_data', lambda path: False)
    assert engine._strategy_aux_needs(_FakeStrat('S_no_reqs_no_aux')) == set()


def test_needs_no_source_file_resolvable_fails_open(tmp_path, monkeypatch):
    """A class with no resolvable source file (inspect.getsourcefile
    returns None) is truly unsure -> fail open."""
    import inspect as _inspect
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(_inspect, 'getsourcefile', lambda cls: None)
    assert engine._strategy_aux_needs(_FakeStrat('S_dynamic')) == set(engine._AUX_LAZY_GATED_KINDS)


def test_needs_scan_exception_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    def _boom(path):
        raise RuntimeError('boom')
    monkeypatch.setattr(factor_prescreen, '_module_reads_aux_data', _boom)
    assert engine._strategy_aux_needs(_FakeStrat('S_boom')) == set(engine._AUX_LAZY_GATED_KINDS)


# ── _aux_data_alias_read + MRO scan — the CohortBaseStrategy gap ──
#
# Found live 2026-09-28: strategies.cohort_base.CohortBaseStrategy.
# generate_signals does `aux = aux_data or {}` then `aux.get('macro')` /
# `aux.get('options')` several lines later — _module_reads_aux_data alone
# (which only catches the INLINE aux_data[...] / aux_data.get(...) idiom)
# scans that file and reports False, and a leaf-class-only MRO scan misses
# it entirely, since S_HV14_otm_skew_factor_cohort2026, S_HV15_iv_term_
# structure_cohort2026, S_TR04_zarattini_intraday_spy, S_TR06_baltussen_
# eod_reversal all inherit it and have no requirements.json under their
# exact manifest id.

def _load_module(tmp_path, name, source):
    """Write `source` to tmp_path/{name}.py and import it as a real module
    (inspect.getsourcefile needs a real file backing __module__)."""
    import importlib.util
    path = tmp_path / f'{name}.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_aux_data_alias_read_detects_rename_pattern(tmp_path):
    src = (
        "def generate_signals(prices, regime, universe, aux_data=None):\n"
        "    aux = aux_data or {}\n"
        "    macro = aux.get('macro')\n"
        "    return macro\n"
    )
    mod = _load_module(tmp_path, 'cohort_like', src)
    assert engine._aux_data_alias_read(mod.__file__) is True


def test_aux_data_alias_read_does_not_overmatch_bare_parameter(tmp_path):
    """A strategy that merely ACCEPTS aux_data (never assigns or reads it)
    must not be flagged — matches _module_reads_aux_data's own "accepting
    the parameter is not aux-dependence" rule."""
    src = (
        "def generate_signals(prices, regime, universe, aux_data=None):\n"
        "    return []\n"
    )
    mod = _load_module(tmp_path, 'plain_strat', src)
    assert engine._aux_data_alias_read(mod.__file__) is False


def test_aux_data_alias_read_ignores_unrelated_variable_named_aux(tmp_path):
    """`aux = {}` (not derived from aux_data at all) must not match."""
    src = (
        "def generate_signals(prices, regime, universe, aux_data=None):\n"
        "    aux = {}\n"
        "    return aux.get('macro')\n"
    )
    mod = _load_module(tmp_path, 'unrelated_aux_name', src)
    assert engine._aux_data_alias_read(mod.__file__) is False


def test_strategy_aux_needs_scans_base_class_not_just_leaf(tmp_path, monkeypatch):
    """The actual bug: a strategy's OWN file never touches aux_data, but a
    BASE class in its MRO does (via the alias-rename pattern) — scanning
    only type(strat) would wrongly return set() (fail-closed); scanning the
    whole MRO with both _module_reads_aux_data and _aux_data_alias_read
    correctly fails open."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    base_mod = _load_module(tmp_path, 'fake_cohort_base', (
        "class FakeCohortBase:\n"
        "    def generate_signals(self, prices, regime, universe, aux_data=None):\n"
        "        aux = aux_data or {}\n"
        "        return aux.get('options')\n"
    ))
    leaf_mod = _load_module(tmp_path, 'fake_cohort_leaf', (
        "def generate_signals_leaf_only_helper():\n"
        "    return 1\n"  # this file itself never mentions aux_data at all
    ))
    Leaf = type('Leaf', (base_mod.FakeCohortBase,), {'id': 'S_fake_cohort_leaf'})
    Leaf.__module__ = leaf_mod.__name__
    import sys as _sys
    _sys.modules[leaf_mod.__name__] = leaf_mod    # inspect needs it findable
    _sys.modules[base_mod.__name__] = base_mod
    strat = Leaf()
    assert engine._strategy_aux_needs(strat) == set(engine._AUX_LAZY_GATED_KINDS)


def test_strategy_aux_needs_confident_empty_requires_every_mro_file_resolved(tmp_path, monkeypatch):
    """A clean confident-empty result: every class in the MRO resolves to a
    real file, and NONE of them read aux_data by either detector."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    base_mod = _load_module(tmp_path, 'clean_base', (
        "class CleanBase:\n"
        "    def generate_signals(self, prices, regime, universe, aux_data=None):\n"
        "        return []\n"
    ))
    # type()'s default __module__ is the CALLING frame's module (this test
    # file, which legitimately mentions aux_data all over the place in
    # OTHER tests) — pin it to the clean, isolated fixture module instead
    # so inspect.getsourcefile(Leaf) resolves there, not to this file.
    import sys as _sys
    _sys.modules[base_mod.__name__] = base_mod
    Leaf = type('Leaf', (base_mod.CleanBase,), {'id': 'S_clean_leaf',
                                                '__module__': base_mod.__name__})
    strat = Leaf()
    assert engine._strategy_aux_needs(strat) == set()


def test_needed_aux_kinds_unions_only_running_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_a', required=['prices', 'financials'])
    _write_requirements(tmp_path, 'S_b', required=['prices', 'macro'])
    _write_requirements(tmp_path, 'S_c', required=['prices', 'options_eod'])  # NOT running
    strategies = [_FakeStrat('S_a'), _FakeStrat('S_b'), _FakeStrat('S_c')]
    needed = engine._needed_aux_kinds(strategies, {'S_a', 'S_b'})
    assert needed == {'financials', 'macro'}


def test_log_aux_plan_skip_set_transitioning_no_options_consumer(tmp_path, monkeypatch, caplog):
    """Brief test 2: skip set correct for a TRANSITIONING run set with no
    options strategy — mirrors the real 09-24/09-28 TRANSITIONING acting set
    (13 strategies, categories {prices, insider, earnings} + macro, no
    options consumer — task-1-report.md §(b))."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_ins', required=['prices', 'insider'])
    _write_requirements(tmp_path, 'S_mac', required=['prices', 'macro'])
    strategies = [_FakeStrat('S_ins'), _FakeStrat('S_mac')]
    running = {'S_ins', 'S_mac'}
    needed = engine._needed_aux_kinds(strategies, running)
    with caplog.at_level(logging.INFO):
        engine._log_aux_plan(running, needed)
    msg = next(r.message for r in caplog.records if 'aux plan' in r.message)
    assert 'run=2 strategies' in msg
    assert 'load={insider_txns,macro}' in msg
    assert 'skip={financials,options,prices_30m,sentiment}' in msg


# ═════════════════════════════════════════════════════════════
# _log_aux_plan_drift — plan-vs-run divergence (is_eligible / the crypto
# regime file are read again, independently, inside run_strategies)
# ═════════════════════════════════════════════════════════════

def test_plan_drift_off_mode_never_flags(caplog):
    """running_ids=None (brief kill-switch / 'off' mode: no plan was
    computed) must never produce a drift warning — there is nothing to
    compare against."""
    with caplog.at_level(logging.WARNING):
        drift = engine._log_aux_plan_drift({'S1': [], 'S2': []}, None)
    assert drift == set()
    assert not any('aux plan drift' in r.message for r in caplog.records)


def test_plan_drift_no_divergence_silent(caplog):
    with caplog.at_level(logging.WARNING):
        drift = engine._log_aux_plan_drift({'S1': [], 'S2': []}, {'S1', 'S2'})
    assert drift == set()
    assert not any('aux plan drift' in r.message for r in caplog.records)


def test_plan_drift_flags_unpredicted_strategy(caplog):
    """A strategy ran (appears in strategy_results) that the planner did
    NOT include in the run set — e.g. a regime flip between planning and
    run_strategies made it newly eligible."""
    with caplog.at_level(logging.WARNING):
        drift = engine._log_aux_plan_drift({'S1': [], 'S2': [], 'S3': []}, {'S1', 'S2'})
    assert drift == {'S3'}
    msg = next(r.message for r in caplog.records if 'aux plan drift' in r.message)
    assert "['S3']" in msg


# ═════════════════════════════════════════════════════════════
# OPENCLAW_AUX_LAZY mode selection (constraint 4: default OFF, kill switch)
# ═════════════════════════════════════════════════════════════

@pytest.mark.parametrize('raw,expected', [
    (None, 'off'), ('0', 'off'), ('1', 'on'), ('shadow', 'shadow'),
    ('SHADOW', 'shadow'), ('bogus', 'off'), ('2', 'off'), ('', 'off'),
])
def test_aux_lazy_mode(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv('OPENCLAW_AUX_LAZY', raising=False)
    else:
        monkeypatch.setenv('OPENCLAW_AUX_LAZY', raw)
    assert engine._aux_lazy_mode() == expected


def test_resolve_aux_load_plan_off_computes_nothing(monkeypatch):
    """Brief test 6 (kill switch): OPENCLAW_AUX_LAZY=0 (or unset) must cost
    nothing — not even computing the run set, let alone logging it."""
    monkeypatch.delenv('OPENCLAW_AUX_LAZY', raising=False)
    calls = []
    monkeypatch.setattr(engine, '_running_strategy_ids',
                        lambda *a, **k: calls.append(1) or set())
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan(
        [_FakeStrat('S1')], 'LOW_VOL')
    assert needed_kinds_arg is None
    assert running_ids is None and needed is None
    assert calls == []


def test_resolve_aux_load_plan_shadow_computes_and_logs_but_passes_none(
        tmp_path, monkeypatch, caplog):
    """Brief test 5: shadow mode logs the plan and loads all — the computed
    needed_kinds must NOT reach load_aux_data (needed_kinds_arg is None)."""
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', 'shadow')
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    _write_requirements(tmp_path, 'S1', required=['prices', 'macro'])
    with caplog.at_level(logging.INFO):
        needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan(
            [_FakeStrat('S1')], 'LOW_VOL')
    assert needed_kinds_arg is None            # nothing actually gated
    assert running_ids == {'S1'}
    assert needed == {'macro'}
    assert any('aux plan' in r.message for r in caplog.records)


def test_shadow_mode_end_to_end_all_six_loaders_called(wired, tmp_path, monkeypatch):
    """Brief test 5, end to end: even when the plan says only one kind is
    needed, shadow mode's needed_kinds_arg=None must still make
    load_aux_data call every one of the six loaders — nothing is actually
    skipped."""
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', 'shadow')
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    _write_requirements(tmp_path, 'S1', required=['prices', 'macro'])  # only macro declared
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan(
        [_FakeStrat('S1')], 'LOW_VOL')
    assert needed == {'macro'}                 # the plan itself is narrow
    engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    for kind in engine._AUX_LAZY_GATED_KINDS:
        assert wired[kind], f'{kind} loader was not called under shadow mode'


def test_resolve_aux_load_plan_on_gates_for_real(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    _write_requirements(tmp_path, 'S1', required=['prices', 'macro'])
    with caplog.at_level(logging.INFO):
        needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan(
            [_FakeStrat('S1')], 'LOW_VOL')
    assert needed_kinds_arg == {'macro'}
    assert running_ids == {'S1'}
    assert needed == {'macro'}
    assert any('aux plan' in r.message for r in caplog.records)


# ═════════════════════════════════════════════════════════════
# load_aux_data(needed_kinds=...) — the actual gate, incl. constraint 3
# (last_price only executes when 'options' loads) and constraint 5 (byte-
# identical aux dicts, proven with fake loaders that record their calls)
# ═════════════════════════════════════════════════════════════

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Tiny synthetic data/master/ tree + fake loaders recording every call.

    financials/insider_txns/prices_30m/macro get real, minimal parquet
    content and run through load_aux_data's REAL code path (spied via a
    pd.read_parquet wrapper). 'options' is faked at _load_options_window /
    _apply_options_surface (the options block's own internal complexity —
    HV20, v2 shadow build — is exercised elsewhere, e.g.
    test_engine_options_surface_shadow.py; here only the GATE matters), so
    options_eod.parquet only needs to EXIST. 'sentiment' is faked at
    _sentiment_slice (it is Postgres-backed in production — no DB here).
    """
    monkeypatch.setattr(engine, 'ROOT', tmp_path, raising=False)
    master = tmp_path / 'data' / 'master'
    master.mkdir(parents=True)

    pd.DataFrame([{'ticker': 'AAPL', 'date': '2026-06-01', 'period': 'Q2',
                   'gross_margin': 0.4, 'roe': 0.2,
                   'total_assets': 1000.0, 'working_capital': 100.0}]
                ).to_parquet(master / 'financials.parquet', index=False)
    pd.DataFrame([{'ticker': 'AAPL', 'date': '2026-06-01',
                   'transaction_type': 'BUY', 'insider_name': 'Jane Doe',
                   'net_value': 1000.0, 'shares': 10.0}]
                ).to_parquet(master / 'insider.parquet', index=False)
    (master / 'options_eod.parquet').write_bytes(b'')  # existence only
    pd.DataFrame([{'date': '2026-06-01',
                   'datetime': pd.Timestamp('2026-06-01 09:30'),
                   'ticker': 'AAPL', 'open': 1.0, 'high': 1.0, 'low': 1.0,
                   'close': 1.0, 'volume': 100, 'vwap': 1.0}]
                ).to_parquet(master / 'prices_30m.parquet', index=False)
    pd.DataFrame([{'date': '2026-06-01', 'series': 'VIX', 'value': 14.0}]
                ).to_parquet(master / 'macro.parquet', index=False)

    calls = {'financials': [], 'insider_txns': [], 'options': [],
             'prices_30m': [], 'macro': [], 'sentiment': []}
    _NAME_TO_KIND = {'financials.parquet': 'financials',
                     'insider.parquet': 'insider_txns',
                     'prices_30m.parquet': 'prices_30m',
                     'macro.parquet': 'macro'}

    _rp = engine.pd.read_parquet

    def rp(path, *a, **kw):
        kind = _NAME_TO_KIND.get(Path(path).name)
        if kind:
            calls[kind].append(str(path))
        return _rp(path, *a, **kw)
    monkeypatch.setattr(engine.pd, 'read_parquet', rp)

    def fake_load_options_window(path, columns, window_days, today):
        calls['options'].append(str(path))
        return pd.DataFrame({'ticker': pd.array([], dtype='object'),
                             'expiry': pd.array([], dtype='datetime64[ns]'),
                             'date': pd.array([], dtype='datetime64[ns]')})
    monkeypatch.setattr(engine, '_load_options_window', fake_load_options_window)
    monkeypatch.setattr(engine, '_apply_options_surface', lambda old, *a, **k: old)

    def fake_sentiment(universe, as_of=None):
        calls['sentiment'].append(1)
        return {'AAPL': {'news_count_24h': 3}}
    monkeypatch.setattr(engine, '_sentiment_slice', fake_sentiment)

    return calls


def test_eager_loads_every_kind(wired):
    aux = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=None)
    for kind in engine._AUX_LAZY_GATED_KINDS:
        assert wired[kind], f'{kind} loader was not called in eager mode'
    assert aux['financials']  # {'AAPL': {...}}
    assert aux['insider_txns']
    assert aux['options'] == {}   # empty (faked) options frame -> empty dict
    assert 'prices_30m' in aux
    assert aux['macro']
    assert aux['sentiment'] == {'AAPL': {'news_count_24h': 3}}


def _deep_equal(a, b) -> bool:
    """Recursive equality that tolerates pandas containers nested inside
    dicts (aux['prices_30m'] is a DataFrame; aux['macro'] is
    {series_name: pd.Series}) — plain `==`/dict-`==` raises "truth value of
    a Series/DataFrame is ambiguous" on those instead of comparing."""
    if isinstance(a, pd.DataFrame) or isinstance(b, pd.DataFrame):
        return isinstance(a, pd.DataFrame) and isinstance(b, pd.DataFrame) and a.equals(b)
    if isinstance(a, pd.Series) or isinstance(b, pd.Series):
        return isinstance(a, pd.Series) and isinstance(b, pd.Series) and a.equals(b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def test_load_aux_data_deterministic_when_needed_kinds_is_everything(wired):
    """Sanity check on the `_need()` wrapper itself, NOT the brief's
    constraint-5 proof: needed_kinds=ALL takes the exact same `True` branch
    of `_need()` as needed_kinds=None, so this only shows load_aux_data is
    deterministic given identical inputs — it can't, by construction,
    distinguish a working gate from a gate that does nothing. The REAL
    proof is test_lazy_end_to_end_byte_identical_per_running_strategy
    below, which uses a genuinely NARROWER needed_kinds per run."""
    eager = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=None)
    lazy_all = engine.load_aux_data(['AAPL'], as_of='2026-06-02',
                                    needed_kinds=set(engine._AUX_LAZY_GATED_KINDS))
    assert _deep_equal(eager, lazy_all)


def test_lazy_end_to_end_byte_identical_per_running_strategy(
        wired, tmp_path, monkeypatch):
    """Brief test 1, end to end (constraint 5's literal ask): three
    strategies with DIFFERENT declared requirements, run through the real
    _resolve_aux_load_plan -> load_aux_data -> run_strategies path twice —
    once eager (OPENCLAW_AUX_LAZY unset) and once under OPENCLAW_AUX_LAZY=1
    — and compare what EACH strategy actually received in generate_signals,
    restricted to the kinds it declares (a strategy that never reads a kind
    per its own requirements.json is not expected to see identical values
    for a kind it doesn't consult — only the kinds it actually looks at
    matter for "byte-identical to what this strategy is handed"). Also
    exercises strategy_universes (_slice_aux on the path — SP-7 is ON in
    production) and the calendar step, matching the real run_strategies
    call shape."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_fin', required=['prices', 'financials'])
    _write_requirements(tmp_path, 'S_mac', required=['prices', 'macro'])
    _write_requirements(tmp_path, 'S_opt', required=['prices', 'options_eod'])
    strategies = [_RecordingStrat('S_fin'), _RecordingStrat('S_mac'), _RecordingStrat('S_opt')]

    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)

    prices_panel = pd.DataFrame({'AAPL': [100.0, 101.0]},
                                index=pd.to_datetime(['2026-06-01', '2026-06-02']))
    strategy_universes = {s.id: ['AAPL'] for s in strategies}

    # Eager: OPENCLAW_AUX_LAZY unset -> _resolve_aux_load_plan returns
    # needed_kinds_arg=None -> load_aux_data loads everything.
    monkeypatch.delenv('OPENCLAW_AUX_LAZY', raising=False)
    needed_kinds_arg, _, _ = engine._resolve_aux_load_plan(strategies, 'LOW_VOL')
    assert needed_kinds_arg is None
    aux_eager = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    engine.run_strategies(strategies, prices_panel, {'state': 'LOW_VOL'}, ['AAPL'],
                          aux_eager, strategy_universes=strategy_universes)
    received_eager = {s.id: s.received_aux for s in strategies}
    for s in strategies:
        s.received_aux = None

    # Lazy: OPENCLAW_AUX_LAZY=1 -> a real, narrower needed_kinds.
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan(strategies, 'LOW_VOL')
    assert running_ids == {'S_fin', 'S_mac', 'S_opt'}
    assert needed == {'financials', 'macro', 'options'}   # NOT insider_txns/prices_30m/sentiment
    aux_lazy = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    engine.run_strategies(strategies, prices_panel, {'state': 'LOW_VOL'}, ['AAPL'],
                          aux_lazy, strategy_universes=strategy_universes)
    received_lazy = {s.id: s.received_aux for s in strategies}

    for strat in strategies:
        declared = engine._strategy_aux_needs(strat)   # {'financials'} / {'macro'} / {'options'}
        for kind in declared:
            ea = received_eager[strat.id].get(kind)
            la = received_lazy[strat.id].get(kind)
            assert _deep_equal(ea, la), (
                f'{strat.id}: kind {kind!r} diverged between eager and lazy — '
                f'eager={ea!r} lazy={la!r}')

    # Skipped kinds (nobody declares insider_txns/prices_30m; sentiment
    # kept its "always {}" invariant) are truly absent from the shared aux
    # dict in lazy mode, for every strategy — since load_aux_data builds
    # ONE dict per run, not one per strategy.
    assert 'insider_txns' not in aux_lazy
    assert 'prices_30m' not in aux_lazy
    assert aux_lazy['sentiment'] == {}
    assert 'insider_txns' in aux_eager
    assert 'prices_30m' in aux_eager


@pytest.mark.parametrize('skip_kind', sorted(engine._AUX_LAZY_GATED_KINDS))
def test_lazy_skips_exactly_the_unneeded_kind(wired, skip_kind):
    needed = set(engine._AUX_LAZY_GATED_KINDS) - {skip_kind}
    aux = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed)
    assert wired[skip_kind] == [], f'{skip_kind} loader was called despite not being needed'
    for kind in needed:
        assert wired[kind], f'{kind} loader should have been called (needed)'
    if skip_kind == 'sentiment':
        # Unlike the other kinds, sentiment is unconditional in the eager
        # path (never gated on file existence) -> a skip preserves the
        # "always present" invariant with {} rather than an absent key.
        assert aux['sentiment'] == {}
    else:
        assert skip_kind not in aux


def test_lazy_skip_all_touches_nothing(wired):
    aux = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=set())
    for kind in engine._AUX_LAZY_GATED_KINDS:
        assert wired[kind] == [], f'{kind} loader was called with needed_kinds=set()'
    assert aux.get('financials') is None
    assert aux.get('insider_txns') is None
    assert aux.get('options') is None
    assert aux.get('prices_30m') is None
    assert aux.get('macro') is None
    assert aux['sentiment'] == {}


def test_last_price_read_gated_on_options_kind(wired, monkeypatch):
    """Constraint 3: the last_price read (nested inside the options block)
    must only execute when 'options' is loaded. We can't observe the read
    directly (it's a bare pd.read_parquet('prices.parquet') inside a
    try/except that degrades silently), so instead we prove the GATE: with
    'options' skipped, opts_path.exists() is never even consulted for the
    options block (the whole block, last_price included, is skipped), by
    asserting the options loader (the thing that would have to run before
    last_price could) was never called."""
    aux_skip = engine.load_aux_data(['AAPL'], as_of='2026-06-02',
                                    needed_kinds=set(engine._AUX_LAZY_GATED_KINDS) - {'options'})
    assert wired['options'] == []
    assert 'options' not in aux_skip

    aux_load = engine.load_aux_data(['AAPL'], as_of='2026-06-02',
                                    needed_kinds={'options'})
    assert wired['options']
    assert aux_load['options'] == {}


def test_needed_kinds_none_is_backward_compatible_default(wired):
    """Existing callers (tests/strategies/test_financials_pit_parity.py:
    engine.load_aux_data(['AAA','BBB'], as_of=...)) never pass needed_kinds
    — the new parameter must default to loading everything."""
    aux = engine.load_aux_data(['AAPL'], as_of='2026-06-02')
    for kind in engine._AUX_LAZY_GATED_KINDS:
        assert wired[kind]


# ═════════════════════════════════════════════════════════════
# Fix round 1 (task-3-fix1-brief.md), F1: _resolve_aux_load_plan_safe —
# the planner must never be able to kill the signals step. Blocks shadow.
# ═════════════════════════════════════════════════════════════

def test_resolve_aux_load_plan_safe_passthrough_on_success(tmp_path, monkeypatch):
    """No exception -> identical return to the raw _resolve_aux_load_plan."""
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    _write_requirements(tmp_path, 'S1', required=['prices', 'macro'])
    result = engine._resolve_aux_load_plan_safe([_FakeStrat('S1')], 'LOW_VOL')
    assert result == ({'macro'}, {'S1'}, {'macro'})


def test_resolve_aux_load_plan_safe_falls_open_on_exception(monkeypatch, caplog):
    """F1: the planner raising (registry / regime-gate / crypto-regime-file
    trouble, anywhere inside _resolve_aux_load_plan) must fall back to the
    exact off-mode tuple (None, None, None) — never propagate — with
    exactly ONE warning line naming the exception. Tested under 'shadow'
    (the mode F1 explicitly blocks: a shadow-mode failure must not be able
    to kill the signals step either)."""
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', 'shadow')

    def _boom(strategies, regime_state):
        raise RuntimeError('registry unreachable')
    monkeypatch.setattr(engine, '_running_strategy_ids', _boom)
    with caplog.at_level(logging.WARNING):
        result = engine._resolve_aux_load_plan_safe([_FakeStrat('S1')], 'LOW_VOL')
    assert result == (None, None, None)
    warnings = [r.message for r in caplog.records if 'aux plan failed' in r.message]
    assert len(warnings) == 1
    assert 'registry unreachable' in warnings[0]
    assert 'loading all kinds' in warnings[0]


def test_resolve_aux_load_plan_safe_fallback_feeds_load_aux_data_everything(
        wired, monkeypatch):
    """The fallback's needed_kinds_arg (None) must actually make
    load_aux_data load every kind — proving the fail-open is load-bearing,
    not just a return-value shape."""
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')

    def _boom(strategies, regime_state):
        raise RuntimeError('boom')
    monkeypatch.setattr(engine, '_running_strategy_ids', _boom)
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan_safe(
        [_FakeStrat('S1')], 'LOW_VOL')
    assert needed_kinds_arg is None and running_ids is None and needed is None
    engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    for kind in engine._AUX_LAZY_GATED_KINDS:
        assert wired[kind], f'{kind} loader was not called after the planner fallback'


def test_main_calls_the_safe_wrapper():
    """Static pin: main() cannot be exercised directly (needs a live DB;
    'never run the engine' is a hard rule for this fix round), so this
    pins the one line that makes F1 load-bearing — main() must call
    _resolve_aux_load_plan_safe, not the raw _resolve_aux_load_plan,
    otherwise the wrapper is dead code and an exception there still kills
    the signals step exactly as before this fix."""
    import inspect
    src = inspect.getsource(engine.main)
    assert '_resolve_aux_load_plan_safe(' in src
    assert 'strategy_results = run_strategies(' in src
    assert 'aux_loaded_kinds=_aux_needed_kinds' in src


# ═════════════════════════════════════════════════════════════
# Fix round 1, F2: _strategy_aux_needs — a requirements.json that EXISTS
# but is malformed/unparseable/non-list must fail OPEN to every gated
# kind, not silently swallow through acting_ingest_plan.load_requirements'
# own prices-only fallback (which made "no file" and "file exists but is
# garbage" indistinguishable, and the OLD code read that fallback as a
# confident prices-only declaration -> an empty needed-kinds set -> fail
# CLOSED). Blocks mode 1.
# ═════════════════════════════════════════════════════════════

def test_needs_malformed_json_fails_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_bad.requirements.json').write_text('{not valid json')
    with caplog.at_level(logging.WARNING):
        needs = engine._strategy_aux_needs(_FakeStrat('S_bad'))
    assert needs == set(engine._AUX_LAZY_GATED_KINDS)
    assert any('malformed' in r.message and 'S_bad' in r.message for r in caplog.records)


def test_needs_non_dict_root_fails_open(tmp_path, monkeypatch):
    """Valid JSON, but the root isn't an object (e.g. a bare list) —
    load_requirements()'s own `data.get(...)` would have raised
    AttributeError, caught by its broad except, and silently returned the
    SAME prices-only shape as a missing file. Must fail open here instead."""
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_list.requirements.json').write_text(json.dumps(['prices', 'macro']))
    assert engine._strategy_aux_needs(_FakeStrat('S_list')) == set(engine._AUX_LAZY_GATED_KINDS)


def test_needs_required_non_list_fails_open(tmp_path, monkeypatch):
    """The exact silent-corruption bug this closes: the OLD
    load_requirements()-based path did `list(reqs['required'])` with no
    type check, so a 'required': 'prices' (a bare string, not a list)
    would have silently become ['p','r','i','c','e','s'] — no exception,
    no fail-open, just garbage categories. Must fail open here instead."""
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_str.requirements.json').write_text(
        json.dumps({'strategy_id': 'S_str', 'required': 'prices', 'optional': []}))
    assert engine._strategy_aux_needs(_FakeStrat('S_str')) == set(engine._AUX_LAZY_GATED_KINDS)


def test_needs_optional_non_list_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_str2.requirements.json').write_text(
        json.dumps({'strategy_id': 'S_str2', 'required': ['prices'], 'optional': 'macro'}))
    assert engine._strategy_aux_needs(_FakeStrat('S_str2')) == set(engine._AUX_LAZY_GATED_KINDS)


def test_needs_malformed_requirements_logs_once_per_strategy(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_bad2.requirements.json').write_text('{not valid json')
    with caplog.at_level(logging.WARNING):
        engine._strategy_aux_needs(_FakeStrat('S_bad2'))
        engine._strategy_aux_needs(_FakeStrat('S_bad2'))
    warnings = [r for r in caplog.records
               if 'malformed' in r.message and 'S_bad2' in r.message]
    assert len(warnings) == 1, 'expected exactly one warning across two calls'


def test_needs_valid_file_unaffected_by_f2(tmp_path, monkeypatch):
    """Sanity: F2 only changes the malformed branch — a well-formed file
    still returns exactly its declared (mapped) kinds (brief: "valid file
    ⇒ exactly its kinds"), matching test_needs_from_requirements_file."""
    monkeypatch.setattr(engine, '_MALFORMED_REQS_WARNED', set())
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_ok', required=['prices', 'options_eod'], optional=['macro'])
    assert engine._strategy_aux_needs(_FakeStrat('S_ok')) == {'options', 'macro'}


# ═════════════════════════════════════════════════════════════
# Fix round 1, F3: run_strategies' hard drift guard — a strategy
# ineligible at PLAN time (_resolve_aux_load_plan_safe, before aux
# loading) and eligible at RUN time (run_strategies' own second,
# independent is_eligible read) must still receive its declared aux kinds,
# loaded late, and emit the same signals an eager run would. Blocks mode 1.
# ═════════════════════════════════════════════════════════════

class _AuxDerivedSignalStrat(_FakeStrat):
    """Emits an actual Signal-shaped object carrying the aux value it
    read, so a test can compare EMITTED SIGNALS (not just the raw aux
    dict) between an eager run and a drifted/late-loaded run — the brief's
    literal F3 ask ('emits the same signals as eager')."""

    def generate_signals(self, prices, regime, universe, aux_data):
        fin = (aux_data or {}).get('financials') or {}
        gm = fin.get('AAPL', {}).get('gross_margin')
        if gm is None:
            return []
        return [SimpleNamespace(direction='FLAT', entry_price=0.0, stop_loss=0.0,
                                target_1=0.0, signal_params={'gross_margin': gm})]


def test_drift_guard_loads_missing_kind_late_and_matches_eager_signals(
        wired, tmp_path, monkeypatch, caplog):
    """Brief F3 test: a strategy ineligible at plan time and eligible at
    run time gets its kinds and emits the same signals as eager."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_drift', required=['prices', 'financials'])
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)

    prices_panel = pd.DataFrame({'AAPL': [100.0, 101.0]},
                                index=pd.to_datetime(['2026-06-01', '2026-06-02']))
    strategy_universes = {'S_drift': ['AAPL']}

    # Eager: OPENCLAW_AUX_LAZY unset, always eligible — the reference signal.
    monkeypatch.delenv('OPENCLAW_AUX_LAZY', raising=False)
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    strat_eager = _AuxDerivedSignalStrat('S_drift')
    aux_eager = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=None)
    results_eager = engine.run_strategies(
        [strat_eager], prices_panel, {'state': 'LOW_VOL'}, ['AAPL'], aux_eager,
        strategy_universes=strategy_universes)
    assert results_eager['S_drift'], 'eager run produced no signal to compare against'
    eager_gm = results_eager['S_drift'][0].signal_params['gross_margin']

    # Lazy + drift: OPENCLAW_AUX_LAZY=1, ineligible at PLAN time, eligible
    # at RUN time (the aux-lazy planner's prediction vs. run_strategies'
    # own second read of is_eligible diverging mid-cycle).
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')
    phase = {'v': 'plan'}

    def _is_eligible(sid, rs):
        return phase['v'] == 'run'
    monkeypatch.setattr(engine, 'is_eligible', _is_eligible)

    strat_lazy = _AuxDerivedSignalStrat('S_drift')
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan_safe(
        [strat_lazy], 'LOW_VOL')
    assert running_ids == set(), 'S_drift must be predicted NOT running at plan time'
    assert needed == set()
    aux_lazy = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    assert 'financials' not in aux_lazy, 'the plan must not have loaded financials'

    phase['v'] = 'run'
    with caplog.at_level(logging.WARNING):
        results_lazy = engine.run_strategies(
            [strat_lazy], prices_panel, {'state': 'LOW_VOL'}, ['AAPL'], aux_lazy,
            strategy_universes=strategy_universes,
            aux_loaded_kinds=needed_kinds_arg, as_of='2026-06-02')

    assert results_lazy['S_drift'], 'drifted strategy produced no signal — ran starved'
    lazy_gm = results_lazy['S_drift'][0].signal_params['gross_margin']
    assert lazy_gm == eager_gm, 'drifted-late-load signal diverged from the eager reference'
    assert wired['financials'], 'financials loader was never called for the late load'

    drift_msgs = [r.message for r in caplog.records if 'aux drift' in r.message]
    assert drift_msgs, 'expected an "aux drift" warning'
    assert 'S_drift' in drift_msgs[0] and 'financials' in drift_msgs[0]


def test_drift_guard_merge_preserves_already_loaded_kinds(
        wired, tmp_path, monkeypatch):
    """Regression for a bug caught in advisor pre-review before this
    landed: load_aux_data(needed_kinds=<missing>) ALWAYS returns
    aux['sentiment']={} whenever 'sentiment' isn't in <missing> (its
    unconditional "always present" invariant) — an early draft merged the
    late-load result with a bare aux_data.update(_late), which would
    silently clobber a sentiment value the plan already loaded FOR REAL
    with that empty placeholder, for every strategy processed after the
    drifted one. S_drift (drifts, needs financials) runs BEFORE S_sent
    (no drift, needs sentiment) in this test's strategy order, so S_sent's
    turn comes strictly after S_drift's late-load merge — this only
    passes if the merge is scoped to just the kinds it actually fetched."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S_drift', required=['prices', 'financials'])
    _write_requirements(tmp_path, 'S_sent', required=['prices', 'sentiment'])
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
    monkeypatch.setenv('OPENCLAW_AUX_LAZY', '1')

    phase = {'v': 'plan'}

    def _is_eligible(sid, rs):
        if sid == 'S_drift':
            return phase['v'] == 'run'
        return True   # S_sent always eligible — no drift for it
    monkeypatch.setattr(engine, 'is_eligible', _is_eligible)

    strategies = [_RecordingStrat('S_drift'), _RecordingStrat('S_sent')]
    needed_kinds_arg, running_ids, needed = engine._resolve_aux_load_plan_safe(
        strategies, 'LOW_VOL')
    assert running_ids == {'S_sent'}
    assert needed == {'sentiment'}
    aux = engine.load_aux_data(['AAPL'], as_of='2026-06-02', needed_kinds=needed_kinds_arg)
    assert aux['sentiment'] == {'AAPL': {'news_count_24h': 3}}, 'sentiment must be loaded for real'
    assert 'financials' not in aux

    prices_panel = pd.DataFrame({'AAPL': [100.0, 101.0]},
                                index=pd.to_datetime(['2026-06-01', '2026-06-02']))
    strategy_universes = {s.id: ['AAPL'] for s in strategies}

    phase['v'] = 'run'
    engine.run_strategies(
        strategies, prices_panel, {'state': 'LOW_VOL'}, ['AAPL'], aux,
        strategy_universes=strategy_universes,
        aux_loaded_kinds=needed_kinds_arg, as_of='2026-06-02')

    s_drift, s_sent = strategies
    assert s_drift.received_aux.get('financials', {}).get('AAPL') is not None, (
        'S_drift did not receive its late-loaded financials')
    assert s_sent.received_aux['sentiment'] == {'AAPL': {'news_count_24h': 3}}, (
        "S_sent's real sentiment was clobbered by S_drift's late-load merge")


def test_drift_guard_inert_in_shadow_mode(tmp_path, monkeypatch):
    """aux_loaded_kinds is None in off/shadow mode (nothing was actually
    skipped, so nothing can have drifted) — the drift guard must not fire
    even for a strategy the planner didn't predict, and run_strategies
    must not require aux_loaded_kinds/as_of to be passed at all (backward
    compatible default for every pre-F3 call site)."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    _write_requirements(tmp_path, 'S1', required=['prices', 'financials'])
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_exit_hook_enabled', lambda: False)
    prices_panel = pd.DataFrame({'AAPL': [100.0]}, index=pd.to_datetime(['2026-06-01']))
    strat = _RecordingStrat('S1')
    # No aux_loaded_kinds/as_of passed — must not raise, must not attempt
    # any late load (there is no universe/as_of context to do it with).
    engine.run_strategies(
        [strat], prices_panel, {'state': 'LOW_VOL'}, ['AAPL'], {'sentiment': {}})
    assert strat.received_aux == {'sentiment': {}}
