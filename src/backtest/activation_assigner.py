#!/usr/bin/env python3
"""src/backtest/activation_assigner.py — Strategy Activation backend.

Derives per-(strategy, regime) SIZER eligibility (`strategy_regime_params.
eligible` — the column `strategy_weights._load_active_strategies` reads)
from each strategy's latest `primary_window=TRUE` unified_backtest run.

Design: docs/archive/superpowers/specs/2026-07-05-strategy-activation-slider-design.md
(superseded) → docs/specs/2026-09-25-activation-bench-relative-spec.md (RULED,
current). Operator directive (2026-09-25, verbatim): "remove the activation
slider entirely and instead activate in regime if strategy sharpe is >= to
beta_spy, so around the same in low vol but looser in transitioning/high vol
and tighter in crisis." Rule (spec §1, §5-A/B):
  eligible[r] = QUALIFIES[r] AND (sharpe[r] >= bench[r]              if the cell is NOT
                                   currently eligible in strategy_regime_params
                                   else sharpe[r] >= bench[r] - ACTIVATION_HYSTERESIS)
  where QUALIFIES[r] is the shared per-regime promotion/activation rule
  (backtest.regime_qualification / promotion_service.js judgeRegimeSleeve):
      sharpe[r] STRICTLY > 0
      AND max_dd_pct[r] <= class ceiling (equity/etp 20, option 30, crypto 70)
      AND trade_count[r] >= 100
  bench[r] = S_beta_spy's (registry `parameters.benchmark_sleeve=true`; the
             literal 'S_beta_spy' only if that lookup fails)
             strategy_backtest_regimes.sharpe for regime r from ITS latest
             primary_window run (see load_bench_sharpe). Fail-safe per
             regime (spec §2): missing sleeve run/row → the last-applied
             vector in pipeline_config.strategy_activation_bench_sharpe;
             neither → DEFAULT_MIN_SHARPE (0.5) + a WARN naming the regime.
             Never widens to "everything eligible". ACTIVATION_HYSTERESIS =
             0.10: a cell that is ALREADY eligible keeps that state until
             sharpe drops a full 0.10 below bench (spec §5-B); the class
             gate legs (>0, DD, trades) are never hysteretic — any of them
             failing deactivates immediately. Instrument class comes from
             the manifest (default equity).
  Benchmark sleeves (registry `parameters.benchmark_sleeve`) are always
  eligible in all four regimes (Amendment 1 D-D1) — the bench rule never
  touches them.

  The `threshold` parameter/CLI flag/`pipeline_config.strategy_activation_
  min_sharpe` slider is ACCEPTED BUT UNUSED for the eligibility rule as of
  the 2026-09-25 spec — it is retired plumbing kept only for backward
  compat (printed header/summary lines the dashboard parses, the persisted
  last-applied marker's `threshold` key, which now carries the LOW_VOL bench
  value instead) until Task 2 of that spec removes it from all 12 call sites
  across the codebase.

Unlike the legacy manifest-writing `eligibility_assigner` (which REFUSES to
wipe `eligible_regimes` to empty), this deriver is authoritative for the
LIVE sizer store and DOES set all 4 canonical regimes to eligible=FALSE
when zero regimes clear the bar (auto-dormancy, operator-approved). Manifest
/ registry status is left completely untouched — a strategy can sit
`state=live` with all 4 regimes dormant simultaneously until a later
re-backtest lifts a regime back over threshold.

Only strategies with a latest `primary_window=TRUE` run that ALSO has
>=1 `strategy_backtest_regimes` child row are touched. A strategy with a
`primary_window` row but zero regime rows (malformed/partial backtest
write) is treated the same as "no run at all": SKIPPED, not wiped. A
strategy with no `primary_window` run whatsoever is likewise skipped.
Skipped strategies' `strategy_regime_params` rows are left byte-identical.

Upsert pattern matched verbatim from `src/strategies/eligibility_manager.
py` (~line 158, `set_params`) and `src/channels/api/server.js` (~line
1595, the picker's `strategy_regime_params` sync): same 4-column INSERT +
`ON CONFLICT (strategy_id, regime_state) DO UPDATE SET eligible, set_at,
set_by`, paired with an audit row in `strategy_regime_param_changes`. The
no-op write guard mirrors server.js's `if (before && before.eligible ===
isElig) continue;` EXACTLY: a write is skipped only when a prior row
EXISTS and already matches — a strategy/regime with no prior row always
gets an explicit write (even eligible=False) so eligibility is fully
determined for all 4 canonical regimes, never left implicit-by-omission.

CLI:
  python3 -m backtest.activation_assigner --strategy-id S_xxx [--dry-run] [--min-sharpe X] [--notify]
  python3 -m backtest.activation_assigner --all [--dry-run] [--min-sharpe X] [--notify] [--trigger LABEL]

--dry-run computes the full prior->new diff and prints it but performs NO
database writes whatsoever (the write helper is never called).
--notify (default OFF, so tests/dry-runs stay silent) posts the newly-
dormant + net-change summary to #botjohn-log via the existing webhook
lookup pattern (`agent_registry.webhook_urls->>'botjohn-log'`), mirroring
`regime_blended_sizer._post_corr_cumsharpe_log` / `fold_report.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_qualification import class_thresholds, dd_leg_passes  # noqa: E402

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

CONFIG_KEY = 'strategy_activation_min_sharpe'
DEFAULT_MIN_SHARPE = 0.5

# Activation bench-relative (spec 2026-09-25-activation-bench-relative-spec.md
# §1, §2, §5-A/B; replaces the `sharpe >= threshold` leg above). ACTIVATION_
# HYSTERESIS is the deactivate-only band: a currently-eligible cell keeps its
# state until sharpe falls a full 0.10 below bench (never widens activation --
# the ACTIVATE edge is always the strict `sharpe >= bench` comparison).
ACTIVATION_HYSTERESIS = 0.10
# Fail-safe tier 2 (spec §2): the last-applied bench vector, persisted by
# every successful --all apply so a later run with a missing/stale sleeve
# backtest still has a real comparator instead of falling straight to
# DEFAULT_MIN_SHARPE for every regime.
CONFIG_KEY_BENCH_SHARPE = 'strategy_activation_bench_sharpe'
# Registry `strategy_registry.parameters ->> 'benchmark_sleeve' = 'true'`
# parameter key (same key execution.benchmark_sleeve.PARAM_KEY reads) and the
# literal sleeve id used ONLY when that registry lookup fails or returns
# nothing (spec §1: "sleeve id ... not hard-coded; fall back to the literal
# only if the registry lookup fails").
BENCH_SLEEVE_PARAM_KEY = 'benchmark_sleeve'
BENCH_SLEEVE_FALLBACK_ID = 'S_beta_spy'
# min_trades resolution order (most specific wins):
#   explicit min_trades= / --min-trades  >  pipeline_config  >  class gate (100)
# The pipeline_config key makes the sample-size floor dashboard-adjustable, like
# its min-Sharpe sibling (operator directive 2026-07-16): it was previously
# reachable only from the shared per-regime gate (regime_qualification
# class_thresholds → 100) or a CLI flag, so tuning it needed a code change.
# The old fixed MIN_TRADES=20 is retired.
CONFIG_KEY_MIN_TRADES = 'strategy_activation_min_trades'

ACTOR = 'activation_assigner'

# Last-applied marker (2026-08-22). Stamped by every NON-dry-run `--all` run
# that finished with zero per-strategy errors, so the daily compute chain's
# `activation` step (src/execution/activation_apply.py) can tell whether a
# dashboard slider (min-Sharpe / min-trades) moved since eligibility was last
# derived — if it did, the step re-runs this assigner + a weights rebuild
# BEFORE `signals`, so a slider change always takes effect at the next daily
# cycle instead of waiting for the Mon 00:00 ET weekly refresh. The weekly and
# Sunday-finale runs stamp it too, so a slider already applied by them is not
# re-applied the same day. pipeline_config.updated_at (server clock) is the
# comparison timestamp; the JSON value is for humans + the dashboard.
LAST_APPLIED_KEY = 'strategy_activation_last_applied'


def stamp_last_applied(conn, threshold: float, min_trades, activated: int,
                       deactivated: int, trigger: str = 'manual',
                       bench_sharpe: Optional[dict] = None,
                       bench_run_id=None) -> bool:
    """Upsert the last-applied marker. Non-fatal: returns False (and logs)
    on any failure — a marker miss only costs one redundant (idempotent)
    re-apply at the next daily cycle, never a trading day.

    threshold: kept for old readers of this marker (spec 2026-09-25 Task 1
    brief) -- callers now pass the LOW_VOL bench value here instead of the
    retired slider, since that is the closest single-number analog under
    the new regime-aware rule ("around the same in low vol" per the
    operator directive). bench_sharpe/bench_run_id (NEW, spec §2): the full
    per-regime comparator vector and the S_beta_spy run it came from, so a
    later run can tell whether the sleeve's primary run is newer than what
    was last applied (the re-apply trigger activation_apply.py owns --
    Task 2)."""
    payload = json.dumps({
        'threshold': threshold,
        'min_trades': min_trades,
        'activated_cells': int(activated),
        'deactivated_cells': int(deactivated),
        'trigger': trigger,
        'actor': ACTOR,
        'bench_sharpe': bench_sharpe,
        'bench_run_id': bench_run_id,
    }, sort_keys=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (LAST_APPLIED_KEY, payload,
             'Stamped by activation_assigner after each non-dry-run --all apply '
             '(weekly Mon 00:00 ET, Sunday finale, or the daily-cycle activation '
             'step). updated_at = when eligibility was last derived; the daily '
             'activation step re-applies when a slider row is newer than this.'))
        conn.commit()
        cur.close()
        return True
    except Exception as e:  # pragma: no cover - defensive
        _log(f'WARNING: could not stamp {LAST_APPLIED_KEY}: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def stamp_bench_sharpe_config(conn, bench: dict) -> bool:
    """Persist the resolved bench vector to pipeline_config (spec §2 tier 2
    fail-safe): a later run whose sleeve primary_window run is missing or
    stale falls back to THIS vector instead of jumping straight to
    DEFAULT_MIN_SHARPE for every regime. Same non-fatal contract as
    stamp_last_applied -- called at the identical gate (--all, non-dry-run,
    zero per-strategy errors)."""
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (CONFIG_KEY_BENCH_SHARPE, json.dumps(bench, sort_keys=True),
             "Last-applied S_beta_spy per-regime Sharpe vector (spec "
             '2026-09-25-activation-bench-relative-spec.md §2 tier 2 fail-safe): '
             "used by a later run when it cannot read the benchmark sleeve's "
             'primary_window strategy_backtest_regimes rows. Written by '
             'activation_assigner on every successful --all non-dry-run apply.'))
        conn.commit()
        cur.close()
        return True
    except Exception as e:  # pragma: no cover - defensive
        _log(f'WARNING: could not stamp {CONFIG_KEY_BENCH_SHARPE}: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _load_instrument_classes() -> dict:
    """sid → instrument_class from the manifest (default equity). Read once
    per process; a missing/unreadable manifest degrades to all-equity, which
    only loosens nothing (equity has the tightest DD ceiling)."""
    try:
        m = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        return {sid: (rec.get('instrument_class') or 'equity')
                for sid, rec in (m.get('strategies') or {}).items()}
    except Exception:
        return {}


def _log(msg: str) -> None:
    print(f'[activation_assigner] {msg}', flush=True)


# ── Config accessor ─────────────────────────────────────────────────────────
# Co-located with the deriver (mirrors strategy_weights._get_bt_sharpe_cap's
# placement next to its sole consumer). A future dashboard slider reads the
# same pipeline_config key directly — no separate module needed for that.
def get_activation_threshold(cur) -> float:
    """Read strategy_activation_min_sharpe from pipeline_config; default 0.5.
    Fail-safe: missing row / malformed value / query error all fall back to
    0.5 (mirrors strategy_weights._get_bt_sharpe_cap's fail-safe pattern).
    Callers MUST NOT rely on the connection's transaction state after a
    failure here without rolling back first — see main()'s explicit
    conn.rollback() immediately after calling this, which guarantees a
    clean transaction before the write loop regardless of outcome (2026-
    05-16 tier-1 lesson: an uncaught exception on a SELECT like this left
    the connection aborted for every subsequent statement in
    strategy_weights.py until an explicit rollback was added)."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return DEFAULT_MIN_SHARPE


def get_activation_min_trades(cur) -> Optional[int]:
    """Read strategy_activation_min_trades from pipeline_config.

    Returns None when unset/unreadable, which makes compute_eligible fall back to
    the per-class gate (regime_qualification.class_thresholds → 100). Fail-SAFE
    direction matters: a malformed value must defer to the class floor, never
    loosen to 0 — a 0 floor would activate regimes on a 1-trade sample.

    0 IS honoured when explicitly configured (a deliberate "no sample floor"),
    which is why this returns Optional[int] rather than using 0 as the sentinel.
    Negative values are rejected (they would activate everything).

    Same transaction caveat as get_activation_threshold: callers must rollback
    after a failure before further statements.
    """
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY_MIN_TRADES,))
        row = cur.fetchone()
        if row and row[0] is not None:
            n = int(row[0])
            if n >= 0:
                return n
    except Exception:
        pass
    return None


# ── Bench-relative comparator (spec 2026-09-25-activation-bench-relative) ──
def _load_pipeline_config_bench_vector(conn) -> dict:
    """Read strategy_activation_bench_sharpe (JSON {regime: sharpe}) from
    pipeline_config -- fail-safe tier 2 (spec §2). Fail-safe: missing row /
    malformed value / query error -> {} (caller falls through to
    DEFAULT_MIN_SHARPE per regime). `value` may come back as a jsonb dict
    or as text depending on driver/column config -- handle both."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY_BENCH_SHARPE,))
        row = cur.fetchone()
        cur.close()
        if row and row[0] is not None:
            val = row[0]
            data = val if isinstance(val, dict) else json.loads(val)
            return {r: float(v) for r, v in data.items() if v is not None}
    except Exception:
        pass
    return {}


def load_bench_sharpe(conn, sleeve_id: Optional[str] = None) -> tuple[dict, dict]:
    """Resolve the per-regime benchmark-sleeve Sharpe vector used as the
    activation comparator (spec §1, §2, §5-A).

    Resolved independently PER CANONICAL REGIME (spec §2), so one thin/
    missing regime never widens the whole vector:
      1. `sleeve_id`'s latest primary_window run's strategy_backtest_regimes
         .sharpe (sleeve id: the registry `benchmark_sleeve` parameter --
         S_beta_spy today -- NOT hard-coded; falls back to the literal
         BENCH_SLEEVE_FALLBACK_ID only if that registry lookup fails or
         returns nothing).
      2. else pipeline_config.strategy_activation_bench_sharpe (the vector
         persisted by the last successful --all apply, stamp_bench_sharpe_
         config).
      3. else DEFAULT_MIN_SHARPE (0.5), with a WARN naming the regime.
    Every canonical regime is always present in the returned dict -- never
    a partial vector, never "everything eligible" by omission.

    Returns (bench: {regime: float}, meta): meta = {'sleeve_id',
    'sleeve_source' ('registry'|'literal_fallback'), 'run_id' (the sleeve's
    primary_window run id, or None if no run was found),
    'regime_source': {regime: 'sleeve'|'pipeline_config'|'default'}}.

    sleeve_id: pass the already-resolved id (main() has it from
    execution.benchmark_sleeve.load_benchmark_sleeve_ids, reused here for
    the always-on logic) to skip a second registry round-trip. None
    resolves it with a plain query here -- deliberately NOT
    load_benchmark_sleeve_ids, whose `with conn.cursor() as cur:` usage not
    every caller's cursor stub supports.
    """
    sleeve_source = 'registry'
    if sleeve_id is None:
        cur = conn.cursor()
        ids: set = set()
        try:
            cur.execute("SELECT id FROM strategy_registry WHERE (parameters ->> %s) = 'true'",
                        (BENCH_SLEEVE_PARAM_KEY,))
            ids = {row[0] for row in cur.fetchall()}
        except Exception as e:
            _log(f'bench sleeve id lookup failed ({e}); using literal {BENCH_SLEEVE_FALLBACK_ID}')
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            cur.close()
        if ids:
            sleeve_id = BENCH_SLEEVE_FALLBACK_ID if BENCH_SLEEVE_FALLBACK_ID in ids else sorted(ids)[0]
            if len(ids) > 1:
                _log(f'WARN: multiple benchmark sleeves in the registry {sorted(ids)}; '
                     f'using {sleeve_id} for the activation bench vector')
        else:
            sleeve_id = BENCH_SLEEVE_FALLBACK_ID
            sleeve_source = 'literal_fallback'

    run_id = None
    sleeve_sharpe: dict = {}
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT run_id FROM strategy_backtest_runs
            WHERE strategy_id = %s AND primary_window = TRUE
            ORDER BY run_at DESC LIMIT 1
        """, (sleeve_id,))
        row = cur.fetchone()
        if row:
            run_id = row[0]
            cur.execute("""
                SELECT regime_state, sharpe FROM strategy_backtest_regimes
                WHERE run_id = %s
            """, (run_id,))
            for regime_state, sharpe in cur.fetchall():
                # NUMERIC columns come back as Decimal via psycopg2 -- cast
                # now so every downstream consumer (hysteresis subtraction,
                # json.dumps in the stamps) sees a plain float.
                if sharpe is not None:
                    sleeve_sharpe[regime_state] = float(sharpe)
    except Exception as e:
        _log(f'bench sleeve run lookup failed ({e})')
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()

    fallback_vector = (_load_pipeline_config_bench_vector(conn)
                       if len(sleeve_sharpe) < len(CANONICAL_REGIMES) else {})

    bench: dict = {}
    regime_source: dict = {}
    for r in CANONICAL_REGIMES:
        if r in sleeve_sharpe:
            bench[r] = sleeve_sharpe[r]
            regime_source[r] = 'sleeve'
        elif r in fallback_vector:
            bench[r] = fallback_vector[r]
            regime_source[r] = 'pipeline_config'
            _log(f'{r}: bench sharpe missing from sleeve run {run_id or "<none>"}; '
                 f'using last-applied pipeline_config vector')
        else:
            bench[r] = DEFAULT_MIN_SHARPE
            regime_source[r] = 'default'
            _log(f'WARN: {r} bench sharpe unavailable (no sleeve run, no pipeline_config '
                 f'vector); using DEFAULT_MIN_SHARPE={DEFAULT_MIN_SHARPE}')

    return bench, {'sleeve_id': sleeve_id, 'sleeve_source': sleeve_source,
                   'run_id': run_id, 'regime_source': regime_source}


def _resolve_bench(bench: Optional[dict]) -> dict:
    """Innermost fail-safe layer for direct callers of compute_eligible/
    apply_one that pass a partial or absent bench vector: any regime
    missing from `bench` gets DEFAULT_MIN_SHARPE + a WARN naming it (spec
    §2's final tier). The sleeve/pipeline_config tiering itself happens
    once per run in load_bench_sharpe (main()), not here -- this function
    never touches the DB."""
    bench = bench or {}
    out = {}
    for r in CANONICAL_REGIMES:
        v = bench.get(r)
        if v is None:
            _log(f'WARN: bench sharpe for {r} not supplied; using DEFAULT_MIN_SHARPE={DEFAULT_MIN_SHARPE}')
            out[r] = DEFAULT_MIN_SHARPE
        else:
            out[r] = float(v)
    return out


def _fmt_bench_vector(bench: dict, meta: dict) -> str:
    """Byte-shape for the 'bench:' stdout line (spec §5-E). Deliberately
    does NOT match activation_preview.js's HEADER_RE/DETAIL_RE/SUMMARY_RE
    (no '<token>: LOW_VOL: ' shape, no leading 'threshold=') -- new, unparsed
    output the dashboard's existing assigner-output parser just ignores."""
    return ('bench: sleeve={} source={} run={} '.format(
                meta.get('sleeve_id'), meta.get('sleeve_source'), meta.get('run_id'))
            + ' '.join(f'{r}={bench[r]}' for r in CANONICAL_REGIMES))


def _fmt_bench_diff(gained: dict, lost: dict) -> str:
    """Byte-shape for the 'bench diff:' stdout line (spec §5-E): per-regime
    cells gained/lost this run, the diff the operator acks before the first
    apply."""
    return 'bench diff: ' + ' '.join(
        f'{r} +{gained.get(r, 0)}/-{lost.get(r, 0)}' for r in CANONICAL_REGIMES)


# ── Eligibility computation ─────────────────────────────────────────────────
def _fetch_backtest_rows(conn, strategy_id: str):
    """DB-fetch prefix shared by compute_eligible/apply_one: the strategy's
    latest primary_window run's per-regime rows (chosen universe_shrink_
    metrics sleeves when present, else strategy_backtest_regimes -- W3
    ladder campaign, unchanged). Returns None when there is no
    primary_window run at all, OR the run has zero regime rows (malformed/
    partial write) -- both mean 'skip this strategy', not 'wipe it'."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT run_id FROM strategy_backtest_runs
        WHERE strategy_id = %s AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1
    """, (strategy_id,))
    row = cur.fetchone()
    if not row:
        return None
    run_id = row['run_id']
    # Universe ladder campaign W3 (2026-07-21): when the strategy's live
    # universe is a ladder tier chosen by the shrink pass, judge eligibility
    # on THAT tier's sleeves (universe_shrink_metrics chosen rows) — the
    # full-universe strategy_backtest_regimes sleeves describe a universe the
    # strategy no longer trades. Falls back to the full-run sleeves when no
    # chosen rows exist (no shrink yet / non-ladder predicate).
    cur.execute("""
        SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
        FROM universe_shrink_metrics
        WHERE run_id = %s AND chosen AND regime_state <> 'TOTAL'
    """, (run_id,))
    rows = cur.fetchall()
    if rows:
        _log(f'{strategy_id}: eligibility from chosen shrink sleeves '
             f'({len(rows)} regimes)')
    else:
        cur.execute("""
            SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
            FROM strategy_backtest_regimes
            WHERE run_id = %s
        """, (run_id,))
        rows = cur.fetchall()
    if not rows:
        # primary_window run exists but has zero strategy_backtest_regimes
        # rows (malformed/partial write) -- treat identically to "no run":
        # skip, don't wipe.
        return None
    return rows


def _judge(rows, gate: dict, eff_min_trades: int, bench: dict,
          prior_eligible: dict, always_on: bool) -> tuple[Optional[dict], dict]:
    """Pure per-regime eligibility judgment (no DB access) -- the shared
    class gate AND the bench-relative leg with hysteresis (spec §5-A/B):
        passes = class_gate AND (sharpe >= bench[r]                if not prior_eligible[r]
                                  else sharpe >= bench[r] - ACTIVATION_HYSTERESIS)
    Class-gate failure (sharpe not strictly >0, DD leg, trade floor)
    deactivates immediately regardless of the hysteresis band.

    `bench` MUST already be a fully-resolved {regime: float} vector (see
    _resolve_bench / load_bench_sharpe). `prior_eligible` is {regime:
    True|False|None} -- the strategy's CURRENT strategy_regime_params.
    eligible per cell; None and False both count as "not prior-eligible"
    (only an explicit prior True unlocks the loosened band -- a cell with
    no prior row must clear the full, un-loosened bench).

    Returns (eligible_by_regime, diag) with the same contract as the old
    compute_eligible: diag gains `bench` (the raw bench[r] used) and
    `band_applied` (bool -- True only when the cell is kept eligible
    SOLELY by the hysteresis band: prior True, class gate passing, and
    bench-0.10 <= sharpe < bench)."""
    diag: dict[str, dict] = {}
    for r in rows:
        s = r['sharpe']
        n = r['trade_count'] if r['trade_count'] is not None else 0
        dd = r['max_dd_pct']
        # .get: tolerate legacy fixture rows without a calmar key — missing
        # calmar only forfeits the DD escape hatch (dd_leg_passes contract).
        cal = r.get('calmar') if hasattr(r, 'get') else r['calmar']
        regime = r['regime_state']
        b = bench.get(regime, DEFAULT_MIN_SHARPE)
        pe = prior_eligible.get(regime)
        # QUALIFIES (shared per-regime gate: >0 sharpe, class DD leg — flat
        # ceiling OR Calmar escape hatch under the hard cap (2026-07-27) —
        # trade floor), independent of the comparator/hysteresis below.
        class_gate = (s is not None and dd is not None
                     and s > gate['min_sharpe']
                     and dd_leg_passes(dd, cal, gate)
                     and n >= eff_min_trades)
        if s is None:
            bench_leg = False
            band_applied = False
        else:
            band_floor = (b - ACTIVATION_HYSTERESIS) if pe else b
            bench_leg = s >= band_floor
            band_applied = bool(pe) and class_gate and s < b and s >= (b - ACTIVATION_HYSTERESIS)
        passes = class_gate and bench_leg
        diag[regime] = {'sharpe': s, 'trade_count': n, 'max_dd_pct': dd, 'calmar': cal,
                        'eligible': passes, 'bench': b, 'band_applied': band_applied}
    if not diag:
        return None, {}
    if always_on:
        eligible_by_regime = {r: True for r in CANONICAL_REGIMES}
    else:
        eligible_by_regime = {r: diag.get(r, {}).get('eligible', False) for r in CANONICAL_REGIMES}
    return eligible_by_regime, diag


def compute_eligible(conn, strategy_id: str, threshold: float,
                     min_trades: Optional[int] = None,
                     instrument_class: str = 'equity', *,
                     always_on: bool = False,
                     bench: Optional[dict] = None,
                     prior_eligible: Optional[dict] = None) -> tuple[Optional[dict], dict]:
    """Return (eligible_by_regime, diag) for strategy_id's latest
    primary_window=TRUE run.

    eligible_by_regime: dict of ALL 4 CANONICAL_REGIMES -> bool, fully
    determined (a regime absent from strategy_backtest_regimes counts as
    not-eligible — no observed sharpe/trades). None if the strategy has no
    primary_window run, OR the run has zero regime rows — caller MUST skip
    (not touch strategy_regime_params) in either case.

    diag: per-regime {sharpe, trade_count, max_dd_pct, calmar, eligible,
    bench, band_applied} for every regime row actually present in
    strategy_backtest_regimes (used for the prior->new diff report and the
    audit-row bt_sharpe_after/bt_n_trades columns).

    threshold: retained for CLI/back-compat plumbing only -- NOT used in
    the eligibility rule (spec 2026-09-25 activation bench-relative; see
    module docstring). Slider removal across the other 11 call sites is
    Task 2 of that spec.

    bench: fully-resolved {regime: float} comparator vector, normally
    produced once per run by load_bench_sharpe and threaded down from
    main()/apply_one. None (a direct/test call that doesn't supply one)
    falls back to DEFAULT_MIN_SHARPE for every regime via _resolve_bench,
    with a WARN naming each one -- never "all eligible".

    prior_eligible: {regime: True|False|None} -- the strategy's CURRENT
    strategy_regime_params.eligible per cell, for the hysteresis band (spec
    §5-B). None (the direct-call default) treats every regime as
    not-prior-eligible, i.e. the strict `sharpe >= bench` leg applies to
    every cell -- this function itself never queries strategy_regime_params
    (apply_one supplies the real prior state via _load_prior, sequenced
    AFTER the skip check below so a skipped strategy's params are still
    never touched).

    always_on (Amendment 1 D-D1): benchmark sleeves are eligible in every
    canonical regime regardless of the bench rule; `diag[..]['eligible']`
    still records the bench-relative verdict per regime.
    """
    rows = _fetch_backtest_rows(conn, strategy_id)
    if rows is None:
        return None, {}
    gate = class_thresholds(instrument_class)
    eff_min_trades = gate['min_trades'] if min_trades is None else min_trades
    resolved_bench = _resolve_bench(bench)
    resolved_prior = prior_eligible if prior_eligible is not None else {r: None for r in CANONICAL_REGIMES}
    return _judge(rows, gate, eff_min_trades, resolved_bench, resolved_prior, always_on)


def _load_prior(conn, strategy_id: str) -> dict:
    """Return {regime_state: {eligible, size_scalar, stop_pct, target_pct,
    max_hold_days}} for every strategy_regime_params row that currently
    exists for this strategy. A regime with NO row is simply absent from
    the dict (distinguishes 'never set' from 'set False')."""
    cur = conn.cursor()
    cur.execute("""
        SELECT regime_state, eligible, size_scalar, stop_pct, target_pct, max_hold_days
        FROM strategy_regime_params
        WHERE strategy_id = %s
    """, (strategy_id,))
    out: dict[str, dict] = {}
    for regime, eligible, size_scalar, stop_pct, target_pct, max_hold_days in cur.fetchall():
        out[regime] = {
            'eligible': eligible,
            'size_scalar': float(size_scalar) if size_scalar is not None else None,
            'stop_pct': float(stop_pct) if stop_pct is not None else None,
            'target_pct': float(target_pct) if target_pct is not None else None,
            'max_hold_days': max_hold_days,
        }
    return out


def _classify_action(before_eligible: Optional[bool], new_eligible: bool) -> str:
    """Classify one (before, after) eligible transition.

    'unchanged' ONLY when a prior row exists and its value already matches
    (mirrors the DB-write no-op skip below exactly). A regime with no
    prior row is 'initialized' when the computed value is False (still
    gets an explicit write so eligibility is fully determined) or
    'activated' when the computed value is True."""
    if before_eligible is not None and before_eligible == new_eligible:
        return 'unchanged'
    if before_eligible is True and new_eligible is False:
        return 'deactivated'
    if before_eligible in (False, None) and new_eligible is True:
        return 'activated'
    return 'initialized'


def _apply_regime(cur, strategy_id: str, regime_state: str, new_eligible: bool,
                  prior: dict, sharpe: Optional[float], trade_count: Optional[int],
                  threshold: float, min_trades: int,
                  max_dd_pct: Optional[float] = None,
                  instrument_class: str = 'equity',
                  rule: str = 'qualifies(>0·classDD·trades)+bench_relative') -> str:
    """Write one (strategy, regime) row IFF it actually changes (or has no
    prior row yet). Returns the action taken/would-be-taken. Exact upsert
    pattern matched from eligibility_manager.py / server.js: 4-column
    INSERT + ON CONFLICT (strategy_id, regime_state) DO UPDATE SET
    eligible/set_at/set_by, preserving size_scalar/stop_pct/target_pct/
    max_hold_days verbatim (this deriver owns ONLY the eligible flag)."""
    before = prior.get(regime_state)
    before_eligible = before['eligible'] if before is not None else None
    action = _classify_action(before_eligible, new_eligible)
    if action == 'unchanged':
        return action

    def _row_json(eligible_val):
        return json.dumps({
            'strategy_id': strategy_id, 'regime_state': regime_state,
            'eligible': eligible_val,
            'size_scalar': before['size_scalar'] if before else None,
            'stop_pct': before['stop_pct'] if before else None,
            'target_pct': before['target_pct'] if before else None,
            'max_hold_days': before['max_hold_days'] if before else None,
        })

    before_json = _row_json(before_eligible) if before is not None else None
    after_json = _row_json(new_eligible)
    reason = (f'activation_assigner: sharpe={sharpe} n={trade_count} '
             f'dd={max_dd_pct} class={instrument_class} '
             f'threshold={threshold} min_trades={min_trades} '
             f'rule={rule}')

    cur.execute("""
        INSERT INTO strategy_regime_param_changes
            (actor, strategy_id, regime_state, before_row, after_row,
             reason, source, bt_sharpe_after, bt_n_trades)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
    """, (ACTOR, strategy_id, regime_state, before_json, after_json,
          reason, ACTOR, sharpe, trade_count))
    cur.execute("""
        INSERT INTO strategy_regime_params
            (strategy_id, regime_state, eligible, set_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (strategy_id, regime_state) DO UPDATE
           SET eligible = EXCLUDED.eligible,
               set_at   = NOW(),
               set_by   = EXCLUDED.set_by
    """, (strategy_id, regime_state, new_eligible, ACTOR))
    return action


def apply_one(conn, strategy_id: str, threshold: float,
             dry_run: bool = False, min_trades: Optional[int] = None,
             instrument_class: str = 'equity', *,
             always_on: bool = False, bench: Optional[dict] = None) -> dict:
    """Compute + (unless dry_run) write eligibility for one strategy.

    Returns: {status: 'ok'|'skipped_no_run', strategy_id, prior, new, diag,
    actions}. 'prior'/'new' are {regime_state: bool_or_None} (None = never
    set). 'actions' is {regime_state: action_str}. On 'skipped_no_run',
    prior/new/diag/actions are all {} and NOTHING is touched in the DB —
    not even a read of strategy_regime_params.

    threshold: retained for back-compat only, NOT used in the eligibility
    rule (see compute_eligible's docstring). bench: fully-resolved
    {regime: float} comparator vector -- normally load_bench_sharpe's
    output, loaded ONCE per run by main() and threaded through every
    apply_one call (not re-loaded per strategy). None falls back to
    DEFAULT_MIN_SHARPE for every regime (see _resolve_bench).

    Sequencing (hysteresis needs the strategy's CURRENT eligibility before
    it can judge a new one, unlike the old flat-threshold rule): fetch the
    backtest rows, skip check, THEN _load_prior, THEN judge -- so a
    strategy with no primary_window run still never touches
    strategy_regime_params, and _load_prior runs exactly once (its result
    is reused for both the hysteresis judgment below and the prior->new
    diff at the end of this function -- no second query).
    """
    rows = _fetch_backtest_rows(conn, strategy_id)
    if rows is None:
        return {'status': 'skipped_no_run', 'strategy_id': strategy_id,
                'prior': {}, 'new': {}, 'diag': {}, 'actions': {}}

    gate = class_thresholds(instrument_class)
    eff_min_trades = gate['min_trades'] if min_trades is None else min_trades
    resolved_bench = _resolve_bench(bench)

    prior_rows = _load_prior(conn, strategy_id)
    prior_eligible = {r: (prior_rows[r]['eligible'] if r in prior_rows else None)
                      for r in CANONICAL_REGIMES}

    eligible_by_regime, diag = _judge(rows, gate, eff_min_trades, resolved_bench,
                                      prior_eligible, always_on)
    # rows was non-empty (checked above), so diag/eligible_by_regime are
    # never None here.

    actions: dict[str, str] = {}
    if dry_run:
        for r in CANONICAL_REGIMES:
            actions[r] = _classify_action(prior_eligible[r], eligible_by_regime[r])
    else:
        cur = conn.cursor()
        try:
            for r in CANONICAL_REGIMES:
                d = diag.get(r, {})
                # Audit rows record the per-regime comparator ACTUALLY used
                # (spec §3), not the retired global slider: bench[r], or the
                # hysteresis-loosened bench[r]-0.10 when this cell was kept
                # eligible by the band.
                eff_threshold = resolved_bench.get(r, DEFAULT_MIN_SHARPE)
                if not always_on and prior_eligible.get(r):
                    eff_threshold = eff_threshold - ACTIVATION_HYSTERESIS
                actions[r] = _apply_regime(
                    cur, strategy_id, r, eligible_by_regime[r], prior_rows,
                    sharpe=d.get('sharpe'), trade_count=d.get('trade_count'),
                    threshold=eff_threshold, min_trades=eff_min_trades,
                    max_dd_pct=d.get('max_dd_pct'), instrument_class=instrument_class,
                    rule=('benchmark_sleeve_always_on' if always_on
                          else 'qualifies(>0·classDD·trades)+bench_relative'))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    return {'status': 'ok', 'strategy_id': strategy_id,
            'prior': prior_eligible, 'new': eligible_by_regime,
            'diag': diag, 'actions': actions}


# ── Discord notify (best-effort, never raises) ──────────────────────────────
def _notify_botjohn_log(summary: str, newly_dormant: list, dry_run: bool) -> None:
    """Best-effort post to #botjohn-log. Mirrors regime_blended_sizer.py's
    _post_corr_cumsharpe_log / fold_report.py's webhook pattern: look up
    agent_registry.webhook_urls->>'botjohn-log', POST via urllib with an
    explicit User-Agent (Discord's Cloudflare edge 403s the default
    python-urllib/* UA). NEVER raises — a Discord hiccup must not fail the
    weekly eligibility refresh or a manual CLI run."""
    try:
        url = None
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry "
                        "WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
            row = cur.fetchone()
            url = row[0] if row else None
        if not url:
            _log('notify: no botjohn-log webhook URL found; skipping')
            return
        import urllib.request as _ur
        prefix = '[DRY-RUN] ' if dry_run else ''
        lines = [prefix + summary]
        if newly_dormant:
            lines.append('Newly dormant: ' + ', '.join(newly_dormant))
        content = '\n'.join(lines)[:1900]
        req = _ur.Request(
            url, data=json.dumps({'content': content}).encode(), method='POST',
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'OpenClaw-ActivationAssigner/1.0 (+botjohn)'})
        _ur.urlopen(req, timeout=10).read()
    except Exception as e:
        _log(f'notify: webhook post skipped ({e})')


# ── CLI ──────────────────────────────────────────────────────────────────────
def _fmt_diff(result: dict) -> str:
    parts = []
    for r in CANONICAL_REGIMES:
        action = result['actions'].get(r, 'unchanged')
        suffix = f' ({action})' if action != 'unchanged' else ''
        parts.append(f"{r}: {result['prior'].get(r)}->{result['new'].get(r)}{suffix}")
    return ', '.join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id')
    g.add_argument('--all', action='store_true',
                   help='Run for every strategy with a primary_window backtest')
    ap.add_argument('--dry-run', action='store_true',
                    help='Compute + print prior->new diff only; NO writes')
    ap.add_argument('--min-sharpe', type=float, default=None,
                    help='Override the pipeline_config threshold for this run')
    ap.add_argument('--min-trades', type=int, default=None,
                    help='Override the per-class gate trade floor (default: '
                         'regime_qualification class_thresholds, 100)')
    ap.add_argument('--notify', action='store_true',
                    help="Post the newly-dormant + net-change summary to "
                         "#botjohn-log (default off so tests/dry-runs stay silent)")
    ap.add_argument('--trigger', default='manual',
                    help='Label recorded in the last-applied marker '
                         '(weekly_cron | sunday_auto_approval | daily_cycle | manual)')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
    if not uri:
        _log('POSTGRES_URI not set'); return 1
    conn = psycopg2.connect(uri)

    if args.min_sharpe is not None:
        threshold = args.min_sharpe
    else:
        cur0 = conn.cursor()
        threshold = get_activation_threshold(cur0)
        cur0.close()
        # Guarantee a clean transaction before the write loop regardless of
        # whether the config read above succeeded or raised.
        conn.rollback()

    # min_trades: explicit --min-trades > pipeline_config > per-class gate (None
    # here → compute_eligible applies class_thresholds). Resolved once per run so
    # the dashboard slider takes effect without a deploy, mirroring min-Sharpe.
    resolved_min_trades = args.min_trades
    if resolved_min_trades is None:
        cur1 = conn.cursor()
        resolved_min_trades = get_activation_min_trades(cur1)
        cur1.close()
        conn.rollback()   # same clean-transaction guarantee as above

    if args.strategy_id:
        sids = [args.strategy_id]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window = TRUE")
        sids = sorted(r[0] for r in cur.fetchall())
        cur.close()

    classes = _load_instrument_classes()
    # Amendment 1 D-D1: benchmark sleeves (registry parameters.benchmark_sleeve=true)
    # are eligible in every regime regardless of the bench rule. Fail-open to "no
    # sleeves" (the loader logs); rollback keeps the transaction clean either way.
    try:
        from execution.benchmark_sleeve import load_benchmark_sleeve_ids
        bench_ids = load_benchmark_sleeve_ids(conn)
    except Exception as e:
        _log(f'benchmark sleeve lookup failed ({e}); no always-on strategies this run')
        bench_ids = set()
    try:
        conn.rollback()
    except Exception:
        pass
    if bench_ids:
        _log(f'always_on (benchmark sleeves): {sorted(bench_ids)}')

    # Activation bench-relative (spec 2026-09-25 §1, §2, §5-A/E): resolve the
    # per-regime S_beta_spy comparator vector ONCE per run and thread it
    # through every apply_one call below (not re-loaded per strategy). Reuse
    # bench_ids (already loaded above) to pick the sleeve id -- prefer the
    # literal BENCH_SLEEVE_FALLBACK_ID if it's among the registry's
    # benchmark sleeves (today there's only one), else the alphabetically-
    # first one, with a WARN if there's more than one; an empty bench_ids
    # (lookup failed OR genuinely zero registry sleeves) leaves sleeve_id_for_
    # bench=None so load_bench_sharpe resolves it itself and, failing that,
    # falls back to the literal (spec §1: "not hard-coded ... literal only
    # if the registry lookup fails").
    if bench_ids:
        sleeve_id_for_bench = (BENCH_SLEEVE_FALLBACK_ID if BENCH_SLEEVE_FALLBACK_ID in bench_ids
                               else sorted(bench_ids)[0])
        if len(bench_ids) > 1:
            _log(f'WARN: multiple benchmark sleeves in the registry {sorted(bench_ids)}; '
                 f'using {sleeve_id_for_bench} for the activation bench vector')
    else:
        sleeve_id_for_bench = None
    bench_vector, bench_meta = load_bench_sharpe(conn, sleeve_id=sleeve_id_for_bench)
    try:
        conn.rollback()
    except Exception:
        pass
    _log(_fmt_bench_vector(bench_vector, bench_meta))

    # NOTE: header + summary line formats are PINNED by activation_preview.js
    # (HEADER_RE / SUMMARY_RE, consumed by the dashboard dry-run endpoint) —
    # keep `threshold=… min_trades=… dry_run=… strategies=…` byte-stable.
    # `threshold` here is the retired slider value (back-compat plumbing
    # only, see module docstring) -- the actual comparator is bench_vector,
    # printed above and used inside apply_one.
    # min_trades is uniform across classes (gate floor 100), so one number
    # remains printable even though the rule is class-aware.
    eff_min_trades = (resolved_min_trades if resolved_min_trades is not None
                      else class_thresholds('equity')['min_trades'])
    _log(f'threshold={threshold} min_trades={eff_min_trades} dry_run={args.dry_run} '
        f'strategies={len(sids)}')

    results = []
    n_errors = 0
    for sid in sids:
        try:
            r = apply_one(conn, sid, threshold, dry_run=args.dry_run,
                          min_trades=resolved_min_trades,
                          instrument_class=classes.get(sid, 'equity'),
                          always_on=(sid in bench_ids),
                          bench=bench_vector)
        except Exception as e:
            _log(f'  ERROR {sid}: {e}')
            try:
                conn.rollback()
            except Exception:
                pass
            n_errors += 1
            continue
        results.append(r)
        if r['status'] == 'skipped_no_run':
            _log(f'  SKIP  {sid}: no primary_window backtest with regime rows')
            continue
        _log(f'  {sid}: {_fmt_diff(r)}')

    n_ok = sum(1 for r in results if r['status'] == 'ok')
    n_skipped = sum(1 for r in results if r['status'] == 'skipped_no_run')
    activated_cells = deactivated_cells = 0
    gained_by_regime = {r: 0 for r in CANONICAL_REGIMES}
    lost_by_regime = {r: 0 for r in CANONICAL_REGIMES}
    newly_dormant: list = []
    for r in results:
        if r['status'] != 'ok':
            continue
        for x in CANONICAL_REGIMES:
            if r['actions'].get(x) == 'activated':
                activated_cells += 1
                gained_by_regime[x] += 1
            elif r['actions'].get(x) == 'deactivated':
                deactivated_cells += 1
                lost_by_regime[x] += 1
        was_active = any(r['prior'].get(x) for x in CANONICAL_REGIMES)
        is_active = any(r['new'].get(x) for x in CANONICAL_REGIMES)
        if was_active and not is_active:
            newly_dormant.append(r['strategy_id'])

    # Bench-relative dry-run/apply diff (spec §5-E): the per-regime
    # prior->new gained/lost breakdown the operator acks before the first
    # apply. Printed on both dry-run and real applies (informative either
    # way); NOT part of the pinned header/summary lines above.
    _log(_fmt_bench_diff(gained_by_regime, lost_by_regime))

    summary = (
        f'activation_assigner summary: {n_ok} strategies evaluated, '
        f'{n_skipped} skipped (no corrected backtest), '
        f'{activated_cells} cell(s) activated, {deactivated_cells} cell(s) deactivated, '
        f'{len(newly_dormant)} newly-dormant strateg{"y" if len(newly_dormant) == 1 else "ies"}'
        + (f' ({", ".join(newly_dormant)})' if newly_dormant else '')
        + f', threshold={threshold}, min_trades={eff_min_trades}, dry_run={args.dry_run}, errors={n_errors}'
    )
    _log(summary)

    if args.notify:
        _notify_botjohn_log(summary, newly_dormant, args.dry_run)

    # Markers: only a clean, complete, non-dry-run apply counts as "applied".
    # A run with per-strategy errors leaves the old markers so the next
    # daily cycle retries; a --strategy-id run never represents the whole
    # fleet. `threshold` in the last-applied marker is now the LOW_VOL bench
    # value (spec 2026-09-25 Task 1 brief: "keep the threshold key for the
    # old readers, write the LOW_VOL bench there") -- NOT the retired slider
    # variable used for the printed header/summary lines above, which stays
    # byte-stable for activation_preview.js.
    if args.all and not args.dry_run and n_errors == 0:
        if stamp_last_applied(conn, bench_vector['LOW_VOL'], eff_min_trades, activated_cells,
                              deactivated_cells, trigger=args.trigger,
                              bench_sharpe=bench_vector, bench_run_id=bench_meta.get('run_id')):
            _log(f'stamped {LAST_APPLIED_KEY} (trigger={args.trigger})')
        if stamp_bench_sharpe_config(conn, bench_vector):
            _log(f'stamped {CONFIG_KEY_BENCH_SHARPE} (fail-safe tier 2 vector)')

    conn.close()
    return 1 if n_errors else 0


if __name__ == '__main__':
    sys.exit(main())
