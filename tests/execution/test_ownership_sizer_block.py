"""B3 enforcement: ownership-blocked names can be REDUCED but never ADDED to.

The block reuses the entry-hygiene gate's `_shed` helper, so the guarantee is
structural: not held -> target dropped; flip -> close-only; same-sign increase ->
capped at held; reduce and flatten pass through untouched.
"""
from __future__ import annotations

import datetime as _datetime
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")
po = importlib.import_module("execution.position_ownership")


@pytest.fixture(autouse=True)
def _hygiene_enabled(monkeypatch):
    # tests/execution/conftest.py defaults OPENCLAW_ENTRY_HYGIENE=0 for the e2e
    # sizer harnesses; this file tests the gate, so re-enable it (same as
    # tests/execution/test_entry_hygiene_gate.py).
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "1")


PARAMS = {
    'stopout_cooldown_days': 7.0,
    'risk_exit_cooldown_days': 7.0,
    'entry_min_price_usd': 2.0,
    'entry_min_adv_usd': 400_000.0,
    'entry_participation_frac': 0.01,
}


def _gate(target, broker, *, ownership_blocked=None, ownership_cycle_date=None,
          ownership_enforcing=None):
    # Every lookup is injected: an omitted one falls through to the REAL
    # Postgres on this box (modules under src/execution load .env at import).
    return rbs._apply_entry_hygiene_gate(
        dict(target), broker,
        stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set(),
        ownership_blocked=set(ownership_blocked or ()),
        ownership_cycle_date=ownership_cycle_date,
        ownership_enforcing=ownership_enforcing)


# ── shed semantics ─────────────────────────────────────────────────────────

def test_blocked_ticker_not_held_is_dropped():
    assert "AAA" not in _gate({"AAA": 5000.0}, {}, ownership_blocked=["AAA"])


def test_blocked_ticker_add_is_capped_at_held_size():
    out = _gate({"AAA": 9000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 4000.0


def test_blocked_ticker_reduce_is_allowed():
    out = _gate({"AAA": 1000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 1000.0


def test_blocked_ticker_flatten_is_never_blocked():
    out = _gate({"AAA": 0.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_ticker_flip_becomes_close_only():
    out = _gate({"AAA": -5000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_short_add_is_capped_at_held_size():
    out = _gate({"AAA": -9000.0}, {"AAA": -4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == -4000.0


def test_unblocked_ticker_is_untouched():
    out = _gate({"BBB": 5000.0}, {}, ownership_blocked=["AAA"])
    assert out["BBB"] == 5000.0


def test_empty_blocklist_is_a_passthrough():
    out = _gate({"AAA": 5000.0}, {}, ownership_blocked=[])
    assert out["AAA"] == 5000.0


# ── blocklist resolution ───────────────────────────────────────────────────

_STATUSES = {'AAA': po.STATUS_UNALLOCATED, 'BBB': po.STATUS_OK,
             'CCC': po.STATUS_SHORTFALL}


def test_blocklist_is_empty_without_the_flag(monkeypatch):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_is_empty_when_the_flag_is_not_exactly_one(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "true")
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_holds_every_non_ok_ticker_with_the_flag(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from(_STATUSES) == {"AAA", "CCC"}


def test_blocklist_of_an_empty_status_map_is_empty(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from({}) == set()
    assert rbs._ownership_blocklist_from(None) == set()


def test_blocklist_logs_the_ownership_line_in_report_only_mode(monkeypatch, caplog):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    with caplog.at_level("INFO", logger=rbs.logger.name):
        rbs._ownership_blocklist_from(_STATUSES)
    assert any("[ownership]" in r.getMessage() for r in caplog.records)


def test_flag_on_resolves_to_a_blocklist_the_gate_actually_sheds(monkeypatch):
    """Connects the two halves the other tests exercise separately: the pure
    resolver (env flag -> set) and the gate's shed semantics (injected set ->
    shed). Without this, a resolver change (e.g. returning a dict, or
    lowercased tickers) that breaks the gate's `tkr in ownership_blocked`
    check could pass every other test here while silently blocking nothing
    in production."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    blocked = rbs._ownership_blocklist_from(_STATUSES)
    out = _gate({"AAA": 9000.0, "BBB": 5000.0}, {"AAA": 4000.0},
                ownership_blocked=blocked)
    assert out["AAA"] == 4000.0   # non-ok -> capped at held
    assert out["BBB"] == 5000.0   # ok -> untouched


# ── the gate itself must never reach the DB ────────────────────────────────

def test_gate_defaults_to_no_block_and_never_loads():
    """An omitted ownership_blocked must resolve to the empty set, NOT to a DB
    read: tests/execution/test_entry_hygiene_gate.py and
    test_sameday_premarket_protection.py call this gate without the kwarg and
    their contract is 'no DB access in tests'. Enforcement is supplied by the
    single production call site instead."""
    import inspect
    src = inspect.getsource(rbs._apply_entry_hygiene_gate)
    assert '_load_ownership_blocklist' not in src, \
        'the gate must not load the blocklist; the call site supplies it'
    out = rbs._apply_entry_hygiene_gate(
        {"AAA": 5000.0}, {}, stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set())
    assert out["AAA"] == 5000.0


def test_the_single_production_call_site_supplies_the_blocklist():
    """test_entry_hygiene_gate.py:198 already pins the gate to exactly ONE
    invocation (definition + call = 2 occurrences). Pin that the one invocation
    is the one that passes the blocklist, so a second emission route can never
    silently skip ownership enforcement.

    Fix round 1: _load_ownership_blocklist() now returns (blocklist,
    cycle_date) — the call site unpacks it rather than inlining the call as
    the ownership_blocked= value, so the pin is on the unpack + the kwarg
    both being present, not on one literal substring.

    Fix round 2: _load_ownership_blocklist() now returns a third value (the
    effective enforcing bool), also threaded through as ownership_enforcing=
    — pinned the same way."""
    import inspect
    tail = inspect.getsource(rbs._emit_orders_from_targets)
    assert '_load_ownership_blocklist()' in tail
    assert 'ownership_blocked=' in tail
    assert 'ownership_cycle_date=' in tail
    assert 'ownership_enforcing=' in tail
    assert inspect.getsource(rbs).count('_apply_entry_hygiene_gate(') == 2


def test_the_call_site_exempts_benchmark_sleeve_tickers():
    """Deviation from the brief (recorded in task-8-report.md): this task's
    binding constraints forbid ever blocking a benchmark-sleeve ticker unless
    the brief says otherwise, and the brief is silent — so bench_tkrs must be
    subtracted from the loaded blocklist at the call site, not inside the
    gate (the gate has no bench_tkrs input)."""
    import inspect
    tail = inspect.getsource(rbs._emit_orders_from_targets)
    assert 'set(bench_tkrs or ())' in tail


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1
# ═══════════════════════════════════════════════════════════════════════════

def _applied_lines(caplog):
    return [r.getMessage() for r in caplog.records if 'applied=' in r.getMessage()]


# ── item 1: the applied= line reports EFFECT, not intent ────────────────────

def test_applied_line_reports_zero_when_hygiene_gate_is_off(monkeypatch, caplog):
    """OPENCLAW_ENTRY_HYGIENE=0 short-circuits the gate before the ownership
    branch ever runs, so the effect line must say n_applied=0 even though a
    candidate was passed in — this is the exact bug item 1 fixes: the old
    single log line lived in the resolver and said enforcing=True with
    nothing actually shed."""
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "0")
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 5000.0}, {}, ownership_blocked=["AAA"])
    assert out["AAA"] == 5000.0          # early return: unchanged
    lines = _applied_lines(caplog)
    assert lines and "applied=[]" in lines[-1] and "n_applied=0" in lines[-1]


def test_applied_line_lists_a_blocked_held_add(caplog):
    """A blocked held name with an add is actually shed, so it must be
    PRESENT in applied=."""
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 9000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 4000.0
    lines = _applied_lines(caplog)
    assert lines and "AAA" in lines[-1] and "n_applied=1" in lines[-1]


def test_applied_line_omits_a_ticker_the_call_site_already_exempted(caplog):
    """Bench-exempt names never appear in applied=. The call site
    (test_the_call_site_exempts_benchmark_sleeve_tickers, above) subtracts
    bench_tkrs from the blocklist BEFORE calling this gate — simulated here
    by simply never including the bench ticker in ownership_blocked, exactly
    what that subtraction produces."""
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 9000.0, "SPY": 9000.0},
                    {"AAA": 4000.0, "SPY": 4000.0},
                    ownership_blocked=["AAA"])   # SPY excluded, as if bench-exempted
    assert out["SPY"] == 9000.0          # untouched: never in ownership_blocked
    lines = _applied_lines(caplog)
    assert lines and "SPY" not in lines[-1] and "AAA" in lines[-1]


def test_applied_line_carries_the_cycle_date(caplog):
    out_date = "2026-09-12"
    with caplog.at_level("INFO", logger=rbs.logger.name):
        _gate({"AAA": 9000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"],
              ownership_cycle_date=out_date)
    lines = _applied_lines(caplog)
    assert lines and f"cycle_date={out_date}" in lines[-1]


def test_resolver_line_is_labeled_candidates(monkeypatch, caplog):
    """The resolver's own line reports INTENT — relabeled candidates=/
    n_candidates= (was unlabeled 'not ok=') so it can never be mistaken for
    the gate's applied=/n_applied= effect line."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    with caplog.at_level("INFO", logger=rbs.logger.name):
        rbs._ownership_blocklist_from(_STATUSES)
    lines = [r.getMessage() for r in caplog.records if "[ownership]" in r.getMessage()]
    assert lines
    assert "candidates=" in lines[-1] and "n_candidates=2" in lines[-1]
    assert "applied=" not in lines[-1]


# ── item 2: staleness bound on the blocklist ─────────────────────────────────

_TODAY = _datetime.date(2026, 9, 12)


class _FakeOwnershipCursor:
    """Feeds _load_ownership_blocklist's up-to-two sequential queries: first
    latest_status_map_with_date's SELECT (consumed via fetchall), then — only
    when a cycle_date came back — the CURRENT_DATE - %s age query (consumed
    via fetchone). `responses` is a list of row-lists consumed in call
    order, mirroring tests/execution/test_asset_corr_cap_config.py's
    _FakeCursor/_FakeConn pattern for psycopg2-backed functions."""
    def __init__(self, responses):
        self._responses = list(responses)
        self._current = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        self._current = self._responses.pop(0) if self._responses else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


class _FakeOwnershipConn:
    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeOwnershipCursor(self._responses)


def _patch_ownership_db(monkeypatch, *, status_rows, age_rows=None):
    """status_rows: list of (ticker, status, cycle_date) tuples fed to
    latest_status_map_with_date's query. age_rows: [(age_days,)] fed to the
    CURRENT_DATE - %s query — only consumed when status_rows is non-empty
    (a cycle_date came back)."""
    monkeypatch.setenv("POSTGRES_URI", "postgresql://fake/db")
    import psycopg2
    responses = [status_rows]
    if status_rows:
        responses.append(age_rows if age_rows is not None else [(0,)])
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **k: _FakeOwnershipConn(responses))


def test_load_ownership_blocklist_fresh_ledger_is_unchanged(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    _patch_ownership_db(monkeypatch,
                        status_rows=[("AAA", po.STATUS_UNALLOCATED, _TODAY),
                                     ("BBB", po.STATUS_OK, _TODAY)],
                        age_rows=[(1,)])
    blocked, cycle_date, enforcing = rbs._load_ownership_blocklist()
    assert blocked == {"AAA"}
    assert cycle_date == _TODAY
    assert enforcing is True   # fresh + flag on -> effective enforcing (fix round 2)


def test_load_ownership_blocklist_stale_ledger_fails_open(monkeypatch, caplog):
    """Past OPENCLAW_OWNERSHIP_MAX_AGE_DAYS (default 3) the ledger is treated
    as absent: set() + a WARNING, even with the flag on and real non-ok
    tickers present."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    _patch_ownership_db(monkeypatch,
                        status_rows=[("AAA", po.STATUS_UNALLOCATED, _TODAY)],
                        age_rows=[(4,)])
    with caplog.at_level("WARNING", logger=rbs.logger.name):
        blocked, cycle_date, enforcing = rbs._load_ownership_blocklist()
    assert blocked == set()
    assert cycle_date == _TODAY
    # Fix round 2: a stale ledger is not effectively enforcing even with the
    # flag on — this is what makes the gate's applied= line honest.
    assert enforcing is False
    assert any("stale" in r.getMessage() and "age_days=4" in r.getMessage()
              for r in caplog.records)


def test_load_ownership_blocklist_age_env_override_honoured(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    # age_days=4 is stale at the default (3) but not at an override of 10.
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_MAX_AGE_DAYS", "10")
    _patch_ownership_db(monkeypatch,
                        status_rows=[("AAA", po.STATUS_UNALLOCATED, _TODAY)],
                        age_rows=[(4,)])
    blocked, _, enforcing = rbs._load_ownership_blocklist()
    assert blocked == {"AAA"}
    assert enforcing is True

    # age_days=2 is fresh at the default (3) but stale at an override of 1.
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_MAX_AGE_DAYS", "1")
    _patch_ownership_db(monkeypatch,
                        status_rows=[("AAA", po.STATUS_UNALLOCATED, _TODAY)],
                        age_rows=[(2,)])
    blocked, _, enforcing = rbs._load_ownership_blocklist()
    assert blocked == set()
    assert enforcing is False


def test_load_ownership_blocklist_no_rows_is_empty_as_today(monkeypatch):
    """No rows at all (cycle_date None) skips the age check entirely —
    unchanged from before this fix. Fix round 2: no rows is still
    EFFECTIVELY enforcing when the flag is on — nothing to block is not a
    staleness failure, and this is the one sub-case that reads backwards
    (empty set, yet enforcing=True), so it is pinned explicitly rather than
    discarded with `_`."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    _patch_ownership_db(monkeypatch, status_rows=[])
    blocked, cycle_date, enforcing = rbs._load_ownership_blocklist()
    assert blocked == set()
    assert cycle_date is None
    assert enforcing is True


# ── item 3: fail-open literalism ─────────────────────────────────────────────

class _FakeCtxCursor:
    """Bare context-manager cursor with a fixed, non-stale age response — for
    tests where what raises is a directly-monkeypatched function upstream
    (latest_status_map_with_date) or downstream (_ownership_blocklist_from)
    of this cursor, not the cursor's own query path."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (1,)


class _FakeCtxConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCtxCursor()


def test_load_ownership_blocklist_fails_open_when_the_lookup_raises(monkeypatch, caplog):
    """This is the exact path that runs on main before migration 156 is
    applied (UndefinedTable)."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://fake/db")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _FakeCtxConn())

    def _boom(cur):
        raise RuntimeError('relation "position_ownership" does not exist')
    monkeypatch.setattr(po, "latest_status_map_with_date", _boom)

    with caplog.at_level("WARNING", logger=rbs.logger.name):
        result = rbs._load_ownership_blocklist()
    # Fix round 2: enforcing=False too — a loader exception must never leave
    # the applied= line claiming enforcement.
    assert result == (set(), None, False)
    assert any("latest-status lookup failed" in r.getMessage() for r in caplog.records)


def test_load_ownership_blocklist_fails_open_when_the_resolver_itself_raises(monkeypatch, caplog):
    """Literally item 3: the resolve step (_ownership_blocklist_from) now
    lives INSIDE the try (the prior code called it after the try/except), so
    a raise from the resolve step itself — not just the DB lookup — also
    fails open instead of escaping this function."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://fake/db")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _FakeCtxConn())
    monkeypatch.setattr(po, "latest_status_map_with_date",
                        lambda cur: ({"AAA": po.STATUS_UNALLOCATED}, _TODAY))

    def _boom(*a, **k):
        raise RuntimeError("resolver bug")
    monkeypatch.setattr(rbs, "_ownership_blocklist_from", _boom)

    with caplog.at_level("WARNING", logger=rbs.logger.name):
        result = rbs._load_ownership_blocklist()
    assert result == (set(), None, False)
    assert any("latest-status lookup failed" in r.getMessage() for r in caplog.records)


# ── item 4: full-emit flag-unset identity ────────────────────────────────────

def _stub_hygiene_loaders(monkeypatch):
    """Deterministically neutral entry-hygiene loaders for tests that go
    through the real _emit_orders_from_targets with OPENCLAW_ENTRY_HYGIENE=1
    (this file's autouse fixture) — mirrors the injection style
    tests/execution/test_entry_hygiene_gate.py uses at the gate level, one
    layer up at the loader functions since _emit_orders_from_targets doesn't
    accept these as kwargs."""
    monkeypatch.setattr(rbs, "_load_recent_stopouts", lambda days: {})
    monkeypatch.setattr(rbs, "_load_premarket_vetoes", lambda: set())
    monkeypatch.setattr(rbs, "_load_recent_risk_exits", lambda days: {})
    monkeypatch.setattr(rbs, "_load_liquidity_stats", lambda: None)
    monkeypatch.setattr(rbs, "_load_entry_hygiene_params", lambda: dict(PARAMS))


def test_full_emit_ownership_flag_unset_is_byte_identical_to_flag_set_empty_map(monkeypatch):
    """THIS IS THE LIVE SIZER (fix1 brief hard constraint): with an empty
    ownership ledger, OPENCLAW_OWNERSHIP_BLOCK unset must emit byte-identical
    targets/orders to OPENCLAW_OWNERSHIP_BLOCK=1, through the real
    _emit_orders_from_targets call site. Both legs route through the real
    _ownership_blocklist_from({}) — only the env flag differs — so this
    exercises the actual wiring rather than two independently-stubbed
    outcomes that merely happen to agree."""
    _stub_hygiene_loaders(monkeypatch)
    # Fix round 2: third element is the effective enforcing bool — mirrors
    # the real loader's "no rows -> enforcing = raw flag" case (evaluated
    # lazily so it tracks the env flag this test flips between the two calls
    # below, same as _ownership_blocklist_from({}) already does for blocked).
    monkeypatch.setattr(rbs, "_load_ownership_blocklist",
                        lambda: (rbs._ownership_blocklist_from({}), None,
                                 rbs._ownership_block_on()))

    ticker_meta = {
        'AAA': {'strategies': ['S1'], 'directions': [1],
                'brackets': [{'sid': 'S1', 'direction': 1, 'weight': 5.0,
                              'entry': 50.0, 'stop': 48.0, 't1': 52.5, 't2': None}]},
    }
    kwargs = dict(
        ticker_meta=ticker_meta, nav=100_000.0, confirmer=None, _ortho_groups=None,
        sharpe_by_strat={'S1': 2.0}, eff_weight_by_strat={'S1': 5.0}, opt_active=[],
        weight_by_strat={'S1': 5.0}, scale=1.0, account_state={'nav': 100_000.0},
        broker={},
    )

    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    out_unset = rbs._emit_orders_from_targets({'AAA': 5000.0}, **kwargs)

    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    out_set_empty = rbs._emit_orders_from_targets({'AAA': 5000.0}, **kwargs)

    assert out_unset == out_set_empty
    assert out_unset and out_unset[0]['ticker'] == 'AAA' and out_unset[0]['notional_usd'] == 5000.0


def test_full_emit_bench_ticker_survives_the_ownership_block(monkeypatch, caplog):
    """The gate-level test above (test_applied_line_omits_a_ticker_the_call_
    site_already_exempted) never exercises the actual `- set(bench_tkrs or
    ())` subtraction — it just never puts the bench ticker in
    ownership_blocked to begin with. This one does: _load_ownership_blocklist
    is stubbed to return BOTH tickers as candidates (as if the ledger really
    flagged SPY too), and only the call site's bench_tkrs subtraction is what
    keeps SPY out of the applied gate branch."""
    _stub_hygiene_loaders(monkeypatch)
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    monkeypatch.setattr(rbs, "_load_ownership_blocklist",
                        lambda: ({"SPY", "AAA"}, _TODAY, True))

    ticker_meta = {
        'AAA': {'strategies': ['S1'], 'directions': [1],
                'brackets': [{'sid': 'S1', 'direction': 1, 'weight': 5.0,
                              'entry': 50.0, 'stop': 48.0, 't1': 52.5, 't2': None}]},
        'SPY': {'strategies': ['S_beta_spy'], 'directions': [1],
                'brackets': [{'sid': 'S_beta_spy', 'direction': 1, 'weight': 0.8,
                              'entry': 760.0, 'stop': 456.0, 't1': 3800.0, 't2': 4560.0}]},
    }
    target_usd = {'AAA': 9000.0, 'SPY': 9000.0}
    broker = {'AAA': 4000.0, 'SPY': 4000.0}

    with caplog.at_level("INFO", logger=rbs.logger.name):
        orders = rbs._emit_orders_from_targets(
            target_usd, ticker_meta, nav=100_000.0, confirmer=None, _ortho_groups=None,
            sharpe_by_strat={'S1': 2.0, 'S_beta_spy': 0.8},
            eff_weight_by_strat={'S1': 5.0, 'S_beta_spy': 0.8}, opt_active=[],
            weight_by_strat={'S1': 5.0, 'S_beta_spy': 0.8}, scale=1.0,
            account_state={'nav': 100_000.0}, broker=broker,
            bench_ids={'S_beta_spy'}, bench_tkrs={'SPY'})

    by_tkr = {o['ticker']: o for o in orders}
    # SPY: bench-exempted -> the add survives at the full requested size.
    assert by_tkr['SPY']['target_usd'] == 9000.0
    # AAA: still blocked -> _shed() caps it at held (4000, == broker's current
    # position) -> zero delta -> no order emitted at all (nothing to trade,
    # the add is simply refused). Confirmed instead via the log lines below.
    assert 'AAA' not in by_tkr

    lines = _applied_lines(caplog)
    assert lines
    assert "SPY" not in lines[-1] and "AAA" in lines[-1] and "n_applied=1" in lines[-1]
    # Fix round 2: fresh + flag on + hygiene active -> enforcing=True on the
    # applied= line itself (threaded through from the stubbed loader's third
    # value), not just on the (now-removed) resolver-only read.
    assert "enforcing=True" in lines[-1]


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 2 — enforcing= on the applied= line must be the EFFECTIVE state
# ═══════════════════════════════════════════════════════════════════════════
#
# Fix round 1 left two lies standing on the gate's own applied= line: (a) on
# a stale-ledger cycle the loader already knows nothing was applied
# (enforcing=False), yet the gate re-read the raw env flag and printed
# enforcing=True; (b) with OPENCLAW_ENTRY_HYGIENE=0 the whole gate
# short-circuits (nothing CAN be applied), yet the early return also printed
# the raw flag. Both are fixed by threading the loader's EFFECTIVE boolean
# (`_load_ownership_blocklist()`'s new third return value) through as
# `ownership_enforcing=`, defaulting to `_ownership_block_on()` only when a
# caller omits it (the direct-call tests above and in
# test_entry_hygiene_gate.py / test_sameday_premarket_protection.py).

def test_applied_line_enforcing_false_when_hygiene_gate_off_even_with_fresh_flag_on(
        monkeypatch, caplog):
    """hygiene gate off + flag on + fresh: the early-return line must print
    enforcing=False (literal, not the raw flag) — nothing CAN be applied when
    the gate itself is short-circuited, regardless of flag/ledger state."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "0")
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 5000.0}, {}, ownership_blocked=["AAA"],
                    ownership_enforcing=True)   # as if the loader said "fresh, enforcing"
    assert out["AAA"] == 5000.0   # early return: unchanged
    lines = _applied_lines(caplog)
    assert lines and "enforcing=False" in lines[-1] and "n_applied=0" in lines[-1]


def test_applied_line_enforcing_true_when_fresh_flag_on_and_hygiene_on(caplog, monkeypatch):
    """fresh + flag on + hygiene on: enforcing=True, with applied=[] when
    nothing is actually held/blocked. ownership_enforcing is omitted here to
    also pin the fallback-to-_ownership_block_on() default path."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 5000.0}, {}, ownership_blocked=[])
    assert out["AAA"] == 5000.0
    lines = _applied_lines(caplog)
    assert lines and "applied=[]" in lines[-1] and "n_applied=0" in lines[-1]
    assert "enforcing=True" in lines[-1]


def test_applied_line_enforcing_false_when_flag_off(caplog, monkeypatch):
    """flag off: enforcing=False on the applied= line regardless of what's in
    ownership_blocked or whether the gate is otherwise active."""
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 5000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    lines = _applied_lines(caplog)
    assert lines and "enforcing=False" in lines[-1]


def test_applied_line_enforcing_reflects_stale_ledger_end_to_end(monkeypatch, caplog):
    """stale ledger + flag on: threads the real _load_ownership_blocklist()
    (against a fake stale DB) straight into the gate, and asserts the
    resulting applied= line says enforcing=False — not just that the loader's
    return value is False in isolation (already pinned above), but that
    wiring the two together produces the honest line an operator watches."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    _patch_ownership_db(monkeypatch,
                        status_rows=[("AAA", po.STATUS_UNALLOCATED, _TODAY)],
                        age_rows=[(4,)])
    blocked, cycle_date, enforcing = rbs._load_ownership_blocklist()
    assert enforcing is False   # sanity: this is the stale case

    with caplog.at_level("INFO", logger=rbs.logger.name):
        out = _gate({"AAA": 5000.0}, {"AAA": 4000.0}, ownership_blocked=blocked,
                    ownership_cycle_date=cycle_date, ownership_enforcing=enforcing)
    assert out["AAA"] == 5000.0   # blocked set is empty (stale -> fail open) -> untouched
    lines = _applied_lines(caplog)
    assert lines and "enforcing=False" in lines[-1] and "applied=[]" in lines[-1]


def test_load_ownership_blocklist_exception_enforcing_is_false(monkeypatch, caplog):
    """loader exception ⇒ enforcing=False. Restates
    test_load_ownership_blocklist_fails_open_when_the_lookup_raises's (set(),
    None, False) assertion as an explicit, named pin for this item's own test
    matrix rather than relying on reading it off item 3's test."""
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://fake/db")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _FakeCtxConn())

    def _boom(cur):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(po, "latest_status_map_with_date", _boom)

    with caplog.at_level("WARNING", logger=rbs.logger.name):
        blocked, cycle_date, enforcing = rbs._load_ownership_blocklist()
    assert (blocked, cycle_date, enforcing) == (set(), None, False)
