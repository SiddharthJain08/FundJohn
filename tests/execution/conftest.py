"""Deterministic surfaces for sizer end-to-end tests (2026-07-27).

Modules under src/execution load .env at import, so pytest on the prod box
reaches the REAL Postgres + derived artifacts. Post-hoc target gates
(asset-eligibility a2bdb3e, entry-hygiene + net-exposure cap fix 5/8) then
judge the synthetic fixture tickers against production state — AAA/BBB are
"not in today's Alpaca universe" and every all-long fixture book trips the
net cap. Sizer e2e tests exercise OTHER mechanisms; these gates default OFF
here. Their own unit tests re-enable them explicitly (monkeypatch.setenv /
direct calls with injected inputs).
"""
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")
capped_spawn = importlib.import_module("lib.capped_spawn")


@pytest.fixture(autouse=True)
def _deterministic_sizer_gates(request, monkeypatch):
    # Asset gate: lookup-failure semantics (None → fail-open), matching a box
    # with no DB. Tests that exercise the gate pass `eligibility=` directly.
    monkeypatch.setattr(rbs, '_load_asset_eligibility', lambda symbols: None)
    monkeypatch.setenv('OPENCLAW_NET_EXPOSURE_CAP', '0')
    monkeypatch.setenv('OPENCLAW_ENTRY_HYGIENE', '0')
    # Ownership block (Stream B item 15, task 8): _load_ownership_blocklist()
    # now runs unconditionally at the _emit_orders_from_targets call site (so
    # the `[ownership]` line prints even when OPENCLAW_ENTRY_HYGIENE=0 short-
    # circuits the gate above), which means it opens a REAL
    # psycopg2.connect(POSTGRES_URI) on every e2e test that reaches the
    # emission tail. Stub it the same way as _load_asset_eligibility above.
    # SKIPPED for its own test file, same reasoning as the benchmark stubs
    # below: stubbing the subject-under-test out from under its own unit
    # tests would make them vacuously pass.
    # Fix round 1 item 2: _load_ownership_blocklist() now returns
    # (blocklist, cycle_date) instead of a bare set — the call site unpacks
    # it, so a stub returning a bare set() would raise ValueError (0 elements
    # to unpack into 2 names) in every e2e test that reaches the emission
    # tail. Extended minimally to match.
    # Fix round 2: _load_ownership_blocklist() now returns a third value —
    # the effective enforcing bool — extended again minimally to
    # (set(), None, False), consistent with the flag being unset/empty here.
    if request.path.name != 'test_ownership_sizer_block.py':
        monkeypatch.setattr(rbs, '_load_ownership_blocklist', lambda: (set(), None, False))
    # C1 (Task 6 fix round 1): _apply_account_breaker_gate calls
    # _load_account_breaker_halted() whenever halted is left None (the
    # production path, reached by every e2e test whose target_usd is
    # non-empty at the emission tail) — which, with OPENCLAW_ACCOUNT_BREAKER
    # armed in the environment, opens a REAL psycopg2.connect(POSTGRES_URI).
    # Stub it the same way as the ownership loader above. SKIPPED for its own
    # test file: stubbing the subject-under-test out from under its own unit
    # tests would make the fail-open/flag-gate tests there pass vacuously.
    if request.path.name != 'test_account_breaker_sizer_gate.py':
        monkeypatch.setattr(rbs, '_load_account_breaker_halted', lambda: False)
    # C3 (Task 10): _apply_macro_event_gate calls _load_macro_event_gating()
    # whenever `events` is left None (the production path, reached by every
    # e2e test whose target_usd is non-empty at the emission tail) — which
    # reads the real data/master/macro_events.parquet off disk. Stub it the
    # same way as the two loaders above. SKIPPED for its own test file:
    # stubbing the subject-under-test out from under its own unit tests would
    # make the calendar-failure / non-event-session tests there vacuous.
    # Fix round 1 item 2: _load_macro_event_gating() now returns
    # (events, status) instead of a bare `events` value — the call site
    # unpacks it, so a stub returning a bare None would raise (not iterable)
    # in every e2e test that reaches the emission tail. Extended minimally
    # to (None, 'ok'), consistent with "no event on this session, healthy
    # read".
    if request.path.name != 'test_macro_event_gate.py':
        monkeypatch.setattr(rbs, '_load_macro_event_gating', lambda session: (None, 'ok'))
    # §8 (2026-08-06): production .env carries OPENCLAW_SAMEDAY_SIGNAL_TARGET=1
    # and some test module's import-time load_dotenv pulls it into os.environ
    # during collection. The resolver lets the new flag WIN over the legacy
    # OPENCLAW_EOD_SIGNAL_REGISTER, so every pre-§8 test that enables the T+1
    # mode by monkeypatching only the legacy flag silently stayed in same-day
    # mode (45 combined-run failures, 2026-08-06). Clearing it here restores
    # pure legacy-flag semantics for the suite; §8's own tests set both
    # explicitly.
    monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
    # A1 (final fix wave, 2026-08-29): _sharpe_cadence_path now calls these
    # two on every cycle. The S_m provider (regime_benchmark_sharpe_for_sizing)
    # opens its own psycopg2.connect(POSTGRES_URI) on a cache miss and does an
    # INSERT ... ON CONFLICT DO UPDATE + commit() on pipeline_config; the
    # sleeve loader (load_benchmark_sleeve_ids) also opens a connection. Stub
    # both so sizer tests that don't already patch them stay DB-free and
    # deterministic (no benchmark tickers, no hurdle). Tests that need the
    # real behaviour patch these explicitly (`with _mock.patch(...)`), which
    # overrides this fixture for the duration of the `with` block. SKIPPED for
    # test_benchmark_sleeve.py / test_benchmark_sizing.py themselves — those
    # ARE the unit tests for these two functions (connection-lifecycle,
    # cache-hit/miss, registry parsing); stubbing the subject-under-function
    # out from under its own unit tests would make every one of them
    # vacuously pass/fail against the stub instead of the real body.
    # load_benchmark_horizon (Amendment 1) also opens a connection on the
    # 5-min lane; stubbed to 1.
    if request.path.name not in ('test_benchmark_sleeve.py', 'test_benchmark_sizing.py'):
        monkeypatch.setattr('execution.benchmark_sleeve.load_benchmark_sleeve_ids', lambda conn=None: set())
        monkeypatch.setattr('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', lambda *a, **k: None)
        monkeypatch.setattr('execution.benchmark_sizing.load_benchmark_horizon', lambda *a, **k: 1)


@pytest.fixture(autouse=True)
def _isolate_shadow_log(monkeypatch, tmp_path):
    """lib.shadow_log.record() defaults to ROOT/'logs' — the SAME directory
    scripts/rf_flip_after_fleet.sh and scripts/options_surface_flip_after_shadow.sh
    read (logs/rf_shadow.log, logs/options_surface_shadow.log). Route it into
    a per-test tmp dir so tests that exercise _apply_options_surface (or any
    future execution-layer rf_shadow emitter) never write spurious lines into
    a live flip-gate log. A test that wants to assert on the file overrides
    this env var itself, which wins (monkeypatch is last-write-wins)."""
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))


@pytest.fixture(autouse=True)
def _capped_spawn_hermetic(monkeypatch):
    """QD E2 fix round 2 (task-4 review finding 1): every `run_step()` call
    now goes through `capped_spawn.wrap_capped()`, whose module-global
    `_STATE['available']` defaults to None (unresolved). The FIRST
    unresolved call in a pytest process shells out to a REAL
    `systemd-run --scope --collect --quiet -p MemoryMax=64M -- /bin/true`
    probe (`capped_spawn._real_probe`) — on this box (uid 0, systemd-run
    installed) that is a genuine transient scope, not a mock, created by a
    plain test run. Review caught this: `test_orchestrator_exit_codes.py`
    patches only `pipeline_orchestrator.subprocess.Popen`, which does not
    intercept `capped_spawn`'s OWN `subprocess.run` reference.

    Two independent layers close this for every test under tests/execution/:
      1. OPENCLAW_STEP_MEMORY_MAX=0 makes `wrap_capped`'s own '0'-disables
         short-circuit fire before it ever looks at availability.
      2. `capped_spawn._reset(available=False)` additionally pins the
         (already-unreachable, given #1) availability decision, so any
         future code path that calls `wrap_capped` with an explicit
         non-'0' `memory_max` still can't reach the probe.

    A test that wants the "available" branch (`TestRunStepIsCapped` in
    test_run_lock_wiring.py) overrides both the env var and the pin in its
    own setUp/tearDown — a per-test override that wins for the duration of
    that test. This fixture's teardown calls the bare `_reset()` (unpins to
    None) rather than re-pinning False; that is fine because the NEXT
    test's setup — this same autouse fixture, run again — re-pins False
    before any of that next test's own code executes.

    Belt and suspenders: wrap `capped_spawn.subprocess.run` with a counting
    proxy and assert no call whose argv starts with 'systemd-run' happened
    during the test. This is the direct regression check for the leak the
    review caught — it is scoped to 'systemd-run' specifically (not "any
    subprocess.run call") so it does not false-positive on the many OTHER
    tests in this directory that legitimately shell out via subprocess.run
    (node/python3 smoke tests, docker health checks, etc.) — those calls
    are still forwarded to the real subprocess.run unchanged.
    """
    monkeypatch.setenv('OPENCLAW_STEP_MEMORY_MAX', '0')
    capped_spawn._reset(available=False)

    systemd_calls = []
    real_run = capped_spawn.subprocess.run

    def _counting_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        head = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        if head == 'systemd-run':
            systemd_calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(capped_spawn.subprocess, 'run', _counting_run)
    try:
        yield
    finally:
        capped_spawn._reset()
    assert not systemd_calls, (
        f'capped_spawn triggered {len(systemd_calls)} REAL systemd-run '
        f'call(s) during this test — the exact leak task-4 review finding 1 '
        f'caught (a real 64M scope created on the production box). '
        f'calls={systemd_calls!r}'
    )
