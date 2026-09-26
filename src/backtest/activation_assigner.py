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

  The `threshold` parameter/CLI flag (`--min-sharpe`)/`pipeline_config.
  strategy_activation_min_sharpe` slider is REMOVED as of Task 2 of that
  spec (2026-09-25): `compute_eligible`/`apply_one` no longer accept a
  `threshold` argument at all (a stale positional call raises TypeError,
  not a silent no-op), `get_activation_threshold`/`CONFIG_KEY` are gone,
  and `main()`'s printed header/summary lines display the LOW_VOL bench
  value under the same `threshold=` token for back-compat with anything
  still reading it (byte-shape pinned by activation_preview.js). The
  `pipeline_config.strategy_activation_min_sharpe` row itself is left in
  place, unread (append-only, CLAUDE.md core invariant). The min-TRADES
  slider (`strategy_activation_min_trades`) is UNCHANGED and still reads
  from pipeline_config below.

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
  python3 -m backtest.activation_assigner --strategy-id S_xxx [--dry-run] [--min-trades N] [--notify]
  python3 -m backtest.activation_assigner --all [--dry-run] [--min-trades N] [--notify] [--trigger LABEL]

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
import math
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

# pipeline_config.strategy_activation_min_sharpe (the removed "min-Sharpe
# slider", Task 2 of spec 2026-09-25-activation-bench-relative) is no
# longer read anywhere in this module. The row itself is left in place
# (append-only, CLAUDE.md core invariant) -- just unread. DEFAULT_MIN_SHARPE
# below is unrelated: it is tier 3 of the BENCH fail-safe (spec §2), not
# the old slider's default.
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


def stamp_last_applied(conn, min_trades, activated: int,
                       deactivated: int, trigger: str = 'manual',
                       bench_sharpe: Optional[dict] = None,
                       bench_run_id=None,
                       bench_regime_source: Optional[dict] = None) -> bool:
    """Upsert the last-applied marker. Non-fatal: returns False (and logs)
    on any failure — a marker miss only costs one redundant (idempotent)
    re-apply at the next daily cycle, never a trading day.

    The `threshold` parameter/JSON key (the retired min-Sharpe slider's
    closest single-number analog, LOW_VOL bench -- kept through Task 1 for
    old readers) is REMOVED as of Task 2 (spec 2026-09-25-activation-
    bench-relative §3): bench_sharpe already carries LOW_VOL (and every
    other regime), so the redundant scalar added nothing once nothing
    reads it as "the slider" anymore.

    bench_sharpe/bench_run_id (spec §2): the full per-regime comparator
    vector and the S_beta_spy run it came from, so a later run (execution.
    activation_apply's pending check) can tell whether the sleeve's
    primary run is newer than what was last applied. The hysteresis band
    itself is stored alongside the vector (spec §5-B: "stored with the
    vector in the last-applied stamp") as `bench_hysteresis` --
    ACTIVATION_HYSTERESIS is a code constant today, not
    dashboard-adjustable, but the stamp is the audit trail an operator
    reads to know what band was actually in force for a given apply.

    bench_regime_source (F1-c, fix round 1): load_bench_sharpe's per-regime
    provenance ({regime: 'sleeve'|'pipeline_config'|'default'}) for THIS
    apply's bench_sharpe vector -- lets a later reader (the dashboard bench
    card) tell WHICH regimes were a live sleeve observation vs a degraded
    fallback at apply time, distinct from whether the marker AS A WHOLE
    predates the bench rule (benchCardPayload's `source` field, unrelated)."""
    payload = json.dumps({
        'min_trades': min_trades,
        'activated_cells': int(activated),
        'deactivated_cells': int(deactivated),
        'trigger': trigger,
        'actor': ACTOR,
        'bench_sharpe': bench_sharpe,
        'bench_run_id': bench_run_id,
        'bench_regime_source': bench_regime_source,
        'bench_hysteresis': ACTIVATION_HYSTERESIS,
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
             'activation step re-applies when the min-trades row or the '
             'benchmark sleeve\'s primary backtest run is newer than this.'))
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


def stamp_bench_sharpe_config(conn, bench: dict, regime_source: Optional[dict] = None) -> bool:
    """Persist the resolved bench vector to pipeline_config (spec §2 tier 2
    fail-safe): a later run whose sleeve primary_window run is missing or
    stale falls back to THIS vector instead of jumping straight to
    DEFAULT_MIN_SHARPE for every regime. Same non-fatal contract as
    stamp_last_applied -- called at the identical gate (--all, non-dry-run,
    zero per-strategy errors).

    regime_source: load_bench_sharpe's per-regime provenance
    ({regime: 'sleeve'|'pipeline_config'|'default'}). A regime resolved via
    DEFAULT_MIN_SHARPE THIS run must NOT be written back as if it were a
    real observation -- otherwise the next run reads 0.5 back as a
    legitimate pipeline_config-sourced value instead of hitting tier 3 and
    WARNing again, silencing the fail-safe after exactly one degraded
    apply.

    Review carry-over (2026-09-25 Task 1 review): a regime resolved via
    the tier-2 pipeline_config fallback THIS run must also not be
    re-persisted -- this row's own `updated_at` would refresh to NOW(),
    making a value that is really just being copied forward look like a
    fresh observation forever, which would mask a sleeve backtest that has
    been missing for weeks. The naive fix (persist only sleeve-sourced
    regimes) would instead silently DROP the tier-2 regime from the stored
    JSON row on THIS write (the INSERT/UPDATE replaces the whole value
    column, it doesn't merge) -- demoting it straight to tier-3/DEFAULT on
    the very next run, a bigger behavior change than "don't touch the
    timestamp". So: if ANY regime this run is pipeline_config-sourced, the
    write is skipped ENTIRELY (returns False, logged) and the existing row
    -- vector and updated_at both -- is left exactly as it was. Only when
    every present regime is sleeve-sourced (or default-sourced, which is
    excluded from the payload) does a write happen at all; if that leaves
    nothing to write (every regime defaulted this run), the write is also
    skipped (returns False, logged). regime_source=None (back-compat
    direct callers) persists `bench` as-is, no filtering."""
    to_write = bench
    if regime_source is not None:
        if any(src == 'pipeline_config' for src in regime_source.values()):
            _log(f'{CONFIG_KEY_BENCH_SHARPE}: skipping write -- at least one regime this run '
                 f'is tier-2 (pipeline_config)-sourced; re-persisting it would refresh its '
                 f'updated_at and make a stale fallback look like a fresh observation. '
                 f'Leaving the existing row in place.')
            return False
        to_write = {r: v for r, v in bench.items() if regime_source.get(r) == 'sleeve'}
        if not to_write:
            _log(f'{CONFIG_KEY_BENCH_SHARPE}: nothing to persist (every regime this run '
                 f'came from DEFAULT_MIN_SHARPE)')
            return False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (CONFIG_KEY_BENCH_SHARPE, json.dumps(to_write, sort_keys=True),
             "Last-applied S_beta_spy per-regime Sharpe vector (spec "
             '2026-09-25-activation-bench-relative-spec.md §2 tier 2 fail-safe): '
             "used by a later run when it cannot read the benchmark sleeve's "
             'primary_window strategy_backtest_regimes rows. Written by '
             'activation_assigner on every successful --all non-dry-run apply. '
             'Regimes resolved via DEFAULT_MIN_SHARPE are excluded -- see '
             'stamp_bench_sharpe_config docstring.'))
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


# Module-level buffer of this run's WARN-prefixed lines (fix round 1,
# F1-b): lets main() hand every WARN emitted during the run -- degraded
# tier-2/tier-3 bench fallback, a multi-sleeve registry, a non-finite
# sleeve sharpe -- to the #botjohn-log notify post below, not just the
# summary line. Not read by anything else: activation_preview.js's own
# WARN_RE scans the assigner's raw stdout independently. Cleared at the
# top of main() so repeated in-process calls (tests) never leak warnings
# from one run into the next.
_WARN_LINES: list = []


def _log(msg: str) -> None:
    line = f'[activation_assigner] {msg}'
    print(line, flush=True)
    if msg.startswith('WARN: '):
        _WARN_LINES.append(line)


# ── Config accessor ─────────────────────────────────────────────────────────
# get_activation_threshold (read strategy_activation_min_sharpe -- the
# retired min-Sharpe slider) was REMOVED here (Task 2, spec 2026-09-25-
# activation-bench-relative §3): the eligibility rule no longer reads a
# scalar slider at all, it reads the bench vector (load_bench_sharpe). The
# pipeline_config row itself is left in place, unread (append-only).
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
    malformed top-level value / query error -> {} (caller falls through to
    DEFAULT_MIN_SHARPE per regime). `value` may come back as a jsonb dict
    or as text depending on driver/column config -- handle both.

    Per-entry, not per-dict: a single bad regime value (non-numeric, or
    non-finite -- NaN/±inf, which `json.loads` happily round-trips) must
    not discard the OTHER three, otherwise-valid regimes. Each entry is
    resolved independently; only a malformed top-level payload (not a
    dict, or fails to parse at all) falls back to {} for the whole vector
    (review carry-over, spec 2026-09-25 Task 1 review)."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY_BENCH_SHARPE,))
        row = cur.fetchone()
        cur.close()
        if not row or row[0] is None:
            return {}
        val = row[0]
        data = val if isinstance(val, dict) else json.loads(val)
        if not isinstance(data, dict):
            return {}
        out: dict = {}
        for r, v in data.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            out[r] = fv
        return out
    except Exception:
        pass
    return {}


def resolve_bench_sleeve_id(conn, sleeve_id: Optional[str] = None) -> tuple[str, str]:
    """Resolve the registry's benchmark-sleeve strategy id (`parameters ->>
    'benchmark_sleeve' = 'true'`; S_beta_spy today) -- factored out of
    load_bench_sharpe (Task 2, spec 2026-09-25-activation-bench-relative)
    so OTHER callers can resolve the SAME sleeve id, with the SAME
    tie-break, without re-deriving this logic. Today's only other caller:
    execution.activation_apply's re-apply trigger ("sleeve primary run
    newer than the last-applied marker" -- it needs the sleeve id to look
    up that run, independently of eligibility computation itself).

    Tie-break when the registry has more than one benchmark_sleeve=true
    row: prefer the literal BENCH_SLEEVE_FALLBACK_ID if it's among them,
    else the alphabetically first, with a WARN naming all of them. Falls
    back to the literal outright if the registry lookup fails or returns
    nothing (spec §1: "not hard-coded ... literal only if the registry
    lookup fails").

    Returns (sleeve_id, sleeve_source): sleeve_source is 'registry' (a
    lookup succeeded, OR `sleeve_id` was already supplied by the caller --
    trusted as-if-registry, no query issued -- matches load_bench_sharpe's
    pre-existing contract for that case) or 'literal_fallback' (the lookup
    returned nothing / raised, and BENCH_SLEEVE_FALLBACK_ID was used)."""
    if sleeve_id is not None:
        return sleeve_id, 'registry'
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
        resolved = BENCH_SLEEVE_FALLBACK_ID if BENCH_SLEEVE_FALLBACK_ID in ids else sorted(ids)[0]
        if len(ids) > 1:
            _log(f'WARN: multiple benchmark sleeves in the registry {sorted(ids)}; '
                 f'using {resolved} for the activation bench vector')
        return resolved, 'registry'
    return BENCH_SLEEVE_FALLBACK_ID, 'literal_fallback'


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
    sleeve_id, sleeve_source = resolve_bench_sleeve_id(conn, sleeve_id)

    run_id = None
    sleeve_sharpe: dict = {}
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT run_id FROM strategy_backtest_runs
            WHERE strategy_id = %s AND primary_window = TRUE
            ORDER BY run_at DESC, run_id DESC LIMIT 1
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
                # json.dumps in the stamps) sees a plain float. A None
                # sharpe (production shape for a zero-trade regime in the
                # sleeve's window) is treated as missing for that regime
                # only -- falls through to tier 2/3 below, never widens.
                # Same for a non-finite value (NaN/±inf): never compared,
                # never persisted (review carry-over).
                if sharpe is None:
                    continue
                sharpe = float(sharpe)
                if not math.isfinite(sharpe):
                    _log(f'WARN: sleeve run {run_id} regime {regime_state} sharpe is '
                         f'non-finite ({sharpe}); treating as missing for this regime')
                    continue
                sleeve_sharpe[regime_state] = sharpe
    except Exception as e:
        _log(f'bench sleeve run lookup failed ({e})')
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()

    # Tier-2 trigger: whether to bother querying pipeline_config at all.
    # Per-CANONICAL-REGIME membership, not len(sleeve_sharpe) < 4 -- a stray
    # non-canonical regime_state row (or duplicate) can make the COUNT hit 4
    # while a real canonical regime is still absent, which would silently
    # skip the tier-2 fallback for it (review carry-over).
    missing = [r for r in CANONICAL_REGIMES if r not in sleeve_sharpe]
    fallback_vector = _load_pipeline_config_bench_vector(conn) if missing else {}

    bench: dict = {}
    regime_source: dict = {}
    for r in CANONICAL_REGIMES:
        if r in sleeve_sharpe:
            bench[r] = sleeve_sharpe[r]
            regime_source[r] = 'sleeve'
        elif r in fallback_vector:
            bench[r] = fallback_vector[r]
            regime_source[r] = 'pipeline_config'
            _log(f'WARN: {r} bench sharpe missing from sleeve run {run_id or "<none>"}; '
                 f'using last-applied pipeline_config vector')
        else:
            bench[r] = DEFAULT_MIN_SHARPE
            regime_source[r] = 'default'
            _log(f'WARN: {r} bench sharpe unavailable (no sleeve run, no pipeline_config '
                 f'vector); using DEFAULT_MIN_SHARPE={DEFAULT_MIN_SHARPE}')

    # str(): defensive against a psycopg2 typed adapter (e.g. a registered
    # UUID codec) handing back a non-JSON-serializable object for run_id --
    # this value only ever flows into stamp_last_applied's json.dumps
    # payload downstream, never back into a parameterized query, so the
    # cast is safe. The query above still uses the raw `run_id` variable.
    return bench, {'sleeve_id': sleeve_id, 'sleeve_source': sleeve_source,
                   'run_id': (str(run_id) if run_id is not None else None),
                   'regime_source': regime_source}


def _resolve_bench(bench: Optional[dict]) -> dict:
    """Innermost fail-safe layer for direct callers of compute_eligible/
    apply_one that pass a partial or absent bench vector: any regime
    missing from `bench` -- or present but non-finite (NaN/±inf; review
    carry-over) -- gets DEFAULT_MIN_SHARPE + a WARN naming it (spec §2's
    final tier). The sleeve/pipeline_config tiering itself happens once
    per run in load_bench_sharpe (main()), not here -- this function
    never touches the DB."""
    bench = bench or {}
    out = {}
    for r in CANONICAL_REGIMES:
        v = bench.get(r)
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            else:
                if not math.isfinite(v):
                    v = None
        if v is None:
            _log(f'WARN: bench sharpe for {r} not supplied; using DEFAULT_MIN_SHARPE={DEFAULT_MIN_SHARPE}')
            out[r] = DEFAULT_MIN_SHARPE
        else:
            out[r] = v
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
    compute_eligible: diag gains `bench` (the raw bench[r] used),
    `band_floor` (the rounded, ready-to-compare deactivate-edge: bench[r]
    - ACTIVATION_HYSTERESIS), `band_applied` (bool -- True only when the
    cell is kept eligible SOLELY by the hysteresis band: prior True, class
    gate passing, and band_floor <= sharpe < bench), and `rule` (the
    literal audit-row rule string, spec §3)."""
    diag: dict[str, dict] = {}
    for r in rows:
        s = r['sharpe']
        # NUMERIC (Decimal) vs a float bench compares WRONG at exact
        # equality: Decimal('0.53') >= 0.53 is False (binary 0.53 is not
        # exactly decimal 0.53), even though the strategy's sharpe and the
        # bench are semantically equal. Cast once here so every comparison
        # below (class gate AND the bench leg) is float-float (review
        # carry-over).
        if s is not None:
            s = float(s)
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
        # round(): a plain `b - ACTIVATION_HYSTERESIS` can land on a binary
        # float artifact (0.53 - 0.10 == 0.43000000000000005) that would
        # spuriously deactivate a cell sitting exactly at the intended band
        # floor (review carry-over). Rounded once here and reused for both
        # the comparison and the audit-row threshold (apply_one).
        band_floor = round(b - ACTIVATION_HYSTERESIS, 10)
        if s is None:
            bench_leg = False
            band_applied = False
        else:
            bench_leg = s >= (band_floor if pe else b)
            band_applied = bool(pe) and class_gate and s < b and s >= band_floor
        passes = class_gate and bench_leg
        diag[regime] = {'sharpe': s, 'trade_count': n, 'max_dd_pct': dd, 'calmar': cal,
                        'eligible': passes, 'bench': b, 'band_floor': band_floor,
                        'band_applied': band_applied,
                        'rule': 'qualifies(>0·classDD·trades)+bench_relative'}
    if not diag:
        return None, {}
    if always_on:
        eligible_by_regime = {r: True for r in CANONICAL_REGIMES}
    else:
        eligible_by_regime = {r: diag.get(r, {}).get('eligible', False) for r in CANONICAL_REGIMES}
    return eligible_by_regime, diag


def compute_eligible(conn, strategy_id: str, *,
                     min_trades: Optional[int] = None,
                     instrument_class: str = 'equity',
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
    bench, band_floor, band_applied, rule} for every regime row actually
    present in strategy_backtest_regimes (used for the prior->new diff
    report and the audit-row bt_sharpe_after/bt_n_trades columns).

    Every parameter after `strategy_id` is keyword-only (Task 2, spec
    2026-09-25-activation-bench-relative §3): the retired global
    `threshold` slider parameter that used to sit here has been removed
    entirely (not just unused) so a stale positional call from before the
    slider removal raises a TypeError instead of silently rebinding its
    old `threshold` argument to `min_trades`.

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


def apply_one(conn, strategy_id: str, *,
             dry_run: bool = False, min_trades: Optional[int] = None,
             instrument_class: str = 'equity',
             always_on: bool = False, bench: Optional[dict] = None) -> dict:
    """Compute + (unless dry_run) write eligibility for one strategy.

    Returns: {status: 'ok'|'skipped_no_run', strategy_id, prior, new, diag,
    actions}. 'prior'/'new' are {regime_state: bool_or_None} (None = never
    set). 'actions' is {regime_state: action_str}. On 'skipped_no_run',
    prior/new/diag/actions are all {} and NOTHING is touched in the DB —
    not even a read of strategy_regime_params.

    Every parameter after `strategy_id` is keyword-only (Task 2, spec
    2026-09-25-activation-bench-relative §3): the retired global
    `threshold` slider parameter that used to sit here has been removed
    entirely, so a stale positional call raises TypeError instead of
    silently rebinding into `dry_run`. bench: fully-resolved {regime:
    float} comparator vector -- normally load_bench_sharpe's output,
    loaded ONCE per run by main() and threaded through every apply_one
    call (not re-loaded per strategy). None falls back to
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
                # hysteresis-loosened, rounded band_floor when this cell was
                # kept eligible by the band. Read both from diag (same
                # rounded value _judge already compared against) rather than
                # recomputing here -- a second, unrounded `- ACTIVATION_
                # HYSTERESIS` would drift from the band_floor that actually
                # decided eligibility (review carry-over).
                eff_threshold = d.get('bench', resolved_bench.get(r, DEFAULT_MIN_SHARPE))
                if not always_on and prior_eligible.get(r):
                    eff_threshold = d.get('band_floor', round(eff_threshold - ACTIVATION_HYSTERESIS, 10))
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
def _notify_content(summary: str, newly_dormant: list, dry_run: bool,
                    bench_line: str = '', bench_diff_line: str = '',
                    warn_lines: Optional[list] = None) -> str:
    """Pure assembly of the #botjohn-log post body (F1-b, fix round 1): the
    summary line, the `bench:` vector line, the `bench diff:` line, and
    every WARN line _log collected this run. Before this fix the post
    carried ONLY the summary -- a degraded (tier-2 pipeline_config / tier-3
    DEFAULT_MIN_SHARPE) bench comparator was invisible to the operator
    unless they also happened to read stdout. WARN lines are placed AHEAD
    of the newly-dormant list so the 1900-char Discord clip below drops the
    newly-dormant names (already counted in the summary) before it drops a
    warning. No DB, no network -- pure string-in/string-out, unit-testable
    standalone."""
    prefix = '[DRY-RUN] ' if dry_run else ''
    lines = [prefix + summary]
    if bench_line:
        lines.append(bench_line)
    if bench_diff_line:
        lines.append(bench_diff_line)
    if warn_lines:
        lines.extend(warn_lines)
    if newly_dormant:
        lines.append('Newly dormant: ' + ', '.join(newly_dormant))
    return '\n'.join(lines)[:1900]


def _notify_botjohn_log(summary: str, newly_dormant: list, dry_run: bool,
                        bench_line: str = '', bench_diff_line: str = '',
                        warn_lines: Optional[list] = None) -> None:
    """Best-effort post to #botjohn-log. Mirrors regime_blended_sizer.py's
    _post_corr_cumsharpe_log / fold_report.py's webhook pattern: look up
    agent_registry.webhook_urls->>'botjohn-log', POST via urllib with an
    explicit User-Agent (Discord's Cloudflare edge 403s the default
    python-urllib/* UA). NEVER raises — a Discord hiccup must not fail the
    weekly eligibility refresh or a manual CLI run.

    bench_line/bench_diff_line/warn_lines (F1-b, fix round 1, all optional/
    default-empty for back-compat with any other caller): see
    _notify_content, which does the actual assembly."""
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
        content = _notify_content(summary, newly_dormant, dry_run,
                                  bench_line=bench_line, bench_diff_line=bench_diff_line,
                                  warn_lines=warn_lines)
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
    # F1-b (fix round 1): reset the WARN buffer at the top of every run so
    # repeated in-process calls (tests, or any future long-lived caller)
    # never leak a prior run's warnings into this one's notify post.
    _WARN_LINES.clear()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id')
    g.add_argument('--all', action='store_true',
                   help='Run for every strategy with a primary_window backtest')
    ap.add_argument('--dry-run', action='store_true',
                    help='Compute + print prior->new diff only; NO writes')
    # --min-sharpe (override pipeline_config.strategy_activation_min_sharpe)
    # REMOVED (Task 2, spec 2026-09-25-activation-bench-relative §3): the
    # eligibility rule doesn't read a scalar slider anymore, it reads the
    # per-regime bench vector (load_bench_sharpe) -- there is nothing left
    # for this flag to override.
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
    # Stored, not just logged (F1-b, fix round 1): the notify post below
    # carries this exact text, not a re-derivation of it.
    bench_vector_line = _fmt_bench_vector(bench_vector, bench_meta)
    _log(bench_vector_line)

    # NOTE: header + summary line formats are PINNED by activation_preview.js
    # (HEADER_RE / SUMMARY_RE, consumed by the dashboard dry-run endpoint) —
    # keep `threshold=… min_trades=… dry_run=… strategies=…` byte-stable
    # (Task 2 removed the control it displayed, not the parser -- reshaping
    # this line is a separate dashboard change activation_preview.js's own
    # docstring now calls out, not a Task 2 requirement). `display_threshold`
    # is NOT read by the eligibility rule anywhere -- it is the LOW_VOL bench
    # value, printed purely so this pinned token keeps meaning something to
    # anything still reading it (mirrors the same substitution
    # stamp_last_applied used through Task 1, before its own `threshold` key
    # was dropped in Task 2 -- the JSON stamp had bench_sharpe['LOW_VOL']
    # already; this printed line does not, so it keeps a display copy). The
    # real comparator is bench_vector, printed in full above and used inside
    # apply_one.
    # min_trades is uniform across classes (gate floor 100), so one number
    # remains printable even though the rule is class-aware.
    eff_min_trades = (resolved_min_trades if resolved_min_trades is not None
                      else class_thresholds('equity')['min_trades'])
    display_threshold = bench_vector['LOW_VOL']
    _log(f'threshold={display_threshold} min_trades={eff_min_trades} dry_run={args.dry_run} '
        f'strategies={len(sids)}')

    results = []
    n_errors = 0
    for sid in sids:
        try:
            r = apply_one(conn, sid, dry_run=args.dry_run,
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
    # way); NOT part of the pinned header/summary lines above. Stored, not
    # just logged (F1-b, fix round 1): the notify post below carries this
    # exact text.
    bench_diff_line = _fmt_bench_diff(gained_by_regime, lost_by_regime)
    _log(bench_diff_line)

    summary = (
        f'activation_assigner summary: {n_ok} strategies evaluated, '
        f'{n_skipped} skipped (no corrected backtest), '
        f'{activated_cells} cell(s) activated, {deactivated_cells} cell(s) deactivated, '
        f'{len(newly_dormant)} newly-dormant strateg{"y" if len(newly_dormant) == 1 else "ies"}'
        + (f' ({", ".join(newly_dormant)})' if newly_dormant else '')
        + f', threshold={display_threshold}, min_trades={eff_min_trades}, dry_run={args.dry_run}, errors={n_errors}'
    )
    _log(summary)

    if args.notify:
        # F2 (fix round 1): `summary`'s `threshold=` token above is
        # machine-parsed and byte-pinned (activation_preview.js
        # SUMMARY_RE) -- it stays exactly as printed to stdout. The
        # Discord post is read by a human directly, so relabel just THIS
        # copy to name what the number actually is (the LOW_VOL bench
        # value) without touching `summary` itself or its stdout line.
        notify_summary = summary.replace(
            f'threshold={display_threshold}', f'bench_low_vol={display_threshold}', 1)
        # F1-b: the post used to carry only `summary` -- a degraded (tier-2
        # pipeline_config / tier-3 DEFAULT_MIN_SHARPE) bench comparator was
        # invisible to the operator unless they also read stdout. Now
        # carries the bench: vector line, the bench diff: line, and every
        # WARN: line _log collected this run.
        _notify_botjohn_log(notify_summary, newly_dormant, args.dry_run,
                            bench_line=bench_vector_line, bench_diff_line=bench_diff_line,
                            warn_lines=list(_WARN_LINES))

    # Markers: only a clean, complete, non-dry-run apply counts as "applied".
    # A run with per-strategy errors leaves the old markers so the next
    # daily cycle retries; a --strategy-id run never represents the whole
    # fleet. The last-applied marker's own `threshold` JSON key is GONE as
    # of Task 2 (bench_sharpe already carries LOW_VOL) -- display_threshold
    # above is display-only, for the printed header/summary lines, which
    # stay byte-stable for activation_preview.js.
    if args.all and not args.dry_run and n_errors == 0:
        if stamp_last_applied(conn, eff_min_trades, activated_cells,
                              deactivated_cells, trigger=args.trigger,
                              bench_sharpe=bench_vector, bench_run_id=bench_meta.get('run_id'),
                              bench_regime_source=bench_meta.get('regime_source')):
            _log(f'stamped {LAST_APPLIED_KEY} (trigger={args.trigger})')
        if stamp_bench_sharpe_config(conn, bench_vector, regime_source=bench_meta.get('regime_source')):
            _log(f'stamped {CONFIG_KEY_BENCH_SHARPE} (fail-safe tier 2 vector)')

    conn.close()
    return 1 if n_errors else 0


if __name__ == '__main__':
    sys.exit(main())
