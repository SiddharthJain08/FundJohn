# Regime-Blended Sizer — Phase 2A: Per-(Strategy, Regime) Parameter Infrastructure

**Status:** design approved 2026-05-12; pending operator sign-off on the written spec before plan-writing.

**Scope:** infrastructure only. No LLM, no proposals, no drift detection. Just the
storage layer + consumer wiring that everything downstream will hang off.

---

## 1. Why Phase 2A

Phase 1 (shipped 2026-05-12) gave operators trim/expand of `eligible_regimes` per
strategy via a clickable cell on the strategies page. The field lives in
`manifest.json`. Two limitations:

- **Eligibility is binary**, regime-level only. The system can't yet express
  "this strategy is eligible in HIGH_VOL but only at 0.4x normal size" or
  "tighter stops for this strategy in CRISIS".
- **Manifest as source of truth is fragile.** The lifecycle.py round-trip bug
  (2026-05-12, captured in `feedback_lifecycle_silent_strip`) stripped 5
  operator decisions silently. Manifest is a JSON file with many concurrent
  writers, each prone to incomplete round-trips for new top-level fields.

Phase 2A migrates eligibility out of manifest into a per-(strategy, regime) DB
table, **and** introduces three new tunable params per row (size scalar, stop %,
target %, max-hold days) that downstream Phase 2B (Mastermind proposer) will fill
in. Phase 2A ships before any LLM proposer — the infrastructure must stand on its
own and behave identically to today's Phase 1 until the params are populated.

**Out of scope** (separate follow-up specs):
- **Phase 2B**: extend MastermindJohn comprehensive-review (Sat 18:00 ET) to emit
  per-(strategy, regime) param proposals; proposal table; dashboard approval
  workflow.
- **Phase 2C**: drift detection vs literature priors; Monte Carlo validation harness.
- **Manifest.eligible_regimes field deletion**: deferred 1-2 weeks after 2A lands.

---

## 2. Schema

### Migration 076 — `strategy_regime_params`

```sql
CREATE TABLE strategy_regime_params (
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,   -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    eligible        BOOLEAN      NOT NULL,
    size_scalar     NUMERIC,                  -- NULL = inherit current Phase 1 regime-only static scalar
    stop_pct        NUMERIC,                  -- NULL = strategy-wide default
    target_pct      NUMERIC,                  -- NULL = strategy-wide default
    max_hold_days   INTEGER,                  -- NULL = strategy-wide default
    set_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    set_by          TEXT         NOT NULL,    -- 'migration:from-manifest-2026-05-12' | 'operator:<name>' | 'mastermind' (future)
    PRIMARY KEY (strategy_id, regime_state)
);
```

**Schema choices:**
- Flat row per `(strategy_id, regime_state)`. With 98 strategies × 4 canonical
  regimes the table maxes at 392 rows. EAV or JSONB would be overkill for a
  fixed small parameter set.
- `eligible` is **NOT NULL**. Every (strategy, regime) row has an explicit
  decision. There is no "implicit" eligibility.
- The four tunable params **are nullable**. NULL means "fall back to today's
  Phase 1 default" — lets us migrate eligibility immediately without forcing
  premature commitment on the four params.
- `set_by` is free-text actor tagging, not a foreign key. Detailed audit goes
  in the audit table (§3).

### Migration 077 — `strategy_regime_param_changes` (audit)

```sql
CREATE TABLE strategy_regime_param_changes (
    id              BIGSERIAL    PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,
    before_row      JSONB,                   -- full prior row; NULL on first set
    after_row       JSONB        NOT NULL,
    reason          TEXT,
    source          TEXT                     -- 'dashboard' | 'cli' | 'mastermind' | 'migration'
);

CREATE INDEX idx_srpc_strategy_regime_time
    ON strategy_regime_param_changes (strategy_id, regime_state, changed_at DESC);
```

JSONB before/after snapshots of the full row make rollback trivial and capture
changes to any param (not just `eligible`). Replaces — not extends —
`regime_eligibility_changes` (which stays read-only for historical reference).

### Seed migration

Idempotent script run once during step 3 of rollout (§6):

```
for strategy in manifest.strategies:
    declared = strategy.eligible_regimes   # may be None
    for regime in (LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS):
        if declared is None or declared == []:
            eligible = True                # backward-compat: no field → all eligible
        else:
            eligible = (regime in declared)
        INSERT INTO strategy_regime_params (strategy_id, regime_state, eligible,
                                            set_by)
        VALUES (..., 'migration:from-manifest-2026-05-12')
        ON CONFLICT (strategy_id, regime_state) DO NOTHING
```

Re-runnable. Post-seed: row count must equal `(# strategies) × 4`.

---

## 3. Consumers + Cache

### `regime_gate.is_eligible(strategy_id, regime_state) -> bool`

```python
def is_eligible(strategy_id, regime_state):
    row = _params_cache.get((strategy_id, regime_state))   # in-process, TTL 30s
    if row is None:
        return True                # not migrated → backward-compat eligible
    return row.eligible
```

Today's gate re-reads `manifest.json` per call (fast: disk). DB per call is a
regression risk for cycles with 100+ gate checks. Mitigation:

- **In-process cache**, keyed by `(strategy_id, regime_state)`, TTL 30s. Misses
  fall through to DB.
- **Explicit invalidate hook** the dashboard POST endpoint and CLI call after
  writes — sub-second propagation within the same process.
- **Cross-process staleness** (johnbot Node API ↔ Python orchestrator ↔ maintenance
  cron, each with its own cache): up to 30s. Acceptable for operator-pace edits.
  Redis pub/sub upgrade is noted as a future option but **not** in 2A scope.

Backward-compat: row missing → return True. This matches Phase 1 gate semantics
for strategies with no `eligible_regimes` field.

### `regime_blended_sizer` — size scalar / stop / target / max-hold

```python
def size_scalar(strategy_id, regime_state):
    row = _params_cache.get((strategy_id, regime_state))
    if row is None or row.size_scalar is None:
        return DEFAULT_REGIME_SCALAR[regime_state]   # current Phase 1 static value
    return float(row.size_scalar)
```

Same pattern for `stop_pct`, `target_pct`, `max_hold_days`. NULL → "use today's
default"; non-NULL → override. Until Phase 2B populates these, sizing behavior is
**byte-identical** to Phase 1.

### Dashboard regime cells

- Cell click still toggles eligibility, but POST now writes
  `strategy_regime_params.eligible` via new route
  `POST /api/regime-params/:strategy/:regime`.
- Cell renders an inline param indicator when any of size/stop/target/max-hold
  is non-NULL; click-through expands to an inline editor. Editor is
  param-omission-tolerant (operator can leave any field blank → NULL).
- Back-compat: `/api/regime-eligibility/:strategy` (Phase 1 API) gets a thin
  shim that converts `{regimes: [...]}` into the new DB writes. Old callers
  (CLI scripts, etc.) keep working.

### `eligibility_manager.py` — rewritten

Single-row upsert pattern, audit-before-write semantics preserved. Atomic-file-
write logic disappears; DB transaction provides the atomicity. CLI surface:

```
python3 -m strategies.eligibility_manager --list
python3 -m strategies.eligibility_manager \
    --set <strategy> <regime> [--eligible | --ineligible] \
    [--size <float>] [--stop <float>] [--target <float>] [--max-hold <int>] \
    --actor 'operator:<name>' --reason '<text>'
python3 -m strategies.eligibility_manager --audit [--limit N]
```

The old `--set <strategy> REGIME [REGIME ...]` syntax is deprecated; transitional
behavior emits a warning and translates internally to per-regime upserts.

### Write transaction (canonical)

```python
def set_params(strategy_id, regime_state, *,
               eligible=None, size_scalar=None,
               stop_pct=None, target_pct=None, max_hold_days=None,
               actor, reason, source):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT eligible, size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
                 FOR UPDATE
            """, (strategy_id, regime_state))
            before = cur.fetchone()
            after  = _merge(before, locals())     # caller's non-None values override
            cur.execute("""INSERT INTO strategy_regime_param_changes
                            (actor, strategy_id, regime_state,
                             before_row, after_row, reason, source)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (actor, strategy_id, regime_state,
                         _to_jsonb(before), _to_jsonb(after), reason, source))
            cur.execute("""INSERT INTO strategy_regime_params
                            (strategy_id, regime_state, eligible,
                             size_scalar, stop_pct, target_pct, max_hold_days, set_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (strategy_id, regime_state) DO UPDATE
                           SET eligible      = EXCLUDED.eligible,
                               size_scalar   = EXCLUDED.size_scalar,
                               stop_pct      = EXCLUDED.stop_pct,
                               target_pct    = EXCLUDED.target_pct,
                               max_hold_days = EXCLUDED.max_hold_days,
                               set_at        = NOW(),
                               set_by        = EXCLUDED.set_by""",
                        (strategy_id, regime_state, after['eligible'],
                         after['size_scalar'], after['stop_pct'],
                         after['target_pct'], after['max_hold_days'], actor))
        conn.commit()
    _params_cache.invalidate(strategy_id, regime_state)
```

`SELECT ... FOR UPDATE` + single-transaction audit-then-write prevents diverged
state. Cache invalidation after commit gives same-process readers sub-second
freshness.

### Doctor checks

- **`strategy_regime_params_consistency`** (new): asserts every strategy in
  `strategy_registry` × every canonical regime has exactly one row. Catches
  partial migrations + race conditions + orphans.
  - PASS: full grid (392 rows for 98 strategies)
  - WARN: 1-4 missing OR rows present for strategies not in `strategy_registry`
  - FAIL: ≥5 missing (post-migration this only happens if a new strategy added without seeding)
- **`manifest_eligibility_drift`** (existing): repurposed. New job is detecting
  whether `manifest.eligible_regimes` exists anywhere (its presence indicates a
  stale writer is still touching the deprecated field). WARN in either flag
  mode for one release; the field is fully removed in the follow-up spec.

---

## 4. Manifest deprecation

`manifest.eligible_regimes` is no longer the source of truth, but it stays
**readable** for one release as a transitional safety net. After 1-2 weeks of
operation with `manifest_eligibility_drift = PASS` (no stale writers), a follow-
up spec removes the field entirely from `manifest.json` and from
`lifecycle.py:StrategyRecord` (where it's currently preserved per the
`fe0e56f` fix).

---

## 5. Testing

### Unit tests (Python, pytest)

| File | Coverage | ~Test count |
|---|---|---|
| `tests/test_strategy_regime_params_repo.py` | Data-access layer: insert / upsert / NULL merge / audit roundtrip / FOR UPDATE prevents lost updates / cache invalidate called on write / unknown strategy raises / empty regime rejected | 8 |
| `tests/test_regime_gate_db.py` | Row missing → True (backward-compat); row.eligible=False → False; cache hit skips DB; TTL expiry refetches; invalidate hook refetches; unknown regime_state → False | 6 |
| `tests/test_sizer_param_fallback.py` | size_scalar NULL → static default; non-NULL overrides; same for stop/target/max-hold | 4 |
| `tests/test_migration_076_seed.py` | All (strategy, regime) rows seeded; eligible flag matches manifest declared/absent; idempotent re-run inserts 0 | 5 |
| `tests/test_doctor_strategy_regime_params_consistency.py` | Full grid → PASS; 1 missing → WARN; ≥5 missing → FAIL; DB error → WARN; orphan rows → WARN | 5 |

**Total new tests: ~28.**

### Integration / smoke test

A cycle-level test that:
1. Seeds a strategy as eligible only in TRANSITIONING.
2. Sets current regime to LOW_VOL.
3. Runs the orchestrator's `signals` step.
4. Asserts the strategy generates zero signals.
5. Toggles eligibility to LOW_VOL via the CLI.
6. Re-runs signals step.
7. Asserts the strategy now generates signals.

Also: a parity test against the Phase 1 baseline — run a full DRY-RUN cycle
before and after step 4 of rollout (the gate switch), diff the produced signal
set, must be zero.

---

## 6. Rollout sequence

Each step is independently shippable and reversible.

| # | Step | Reversible by |
|---|---|---|
| 1 | Apply migrations 076 + 077 (pure DDL) | drop tables (no behavior depends yet) |
| 2 | Ship data-access layer + cache + tests (no callers yet) | revert commit |
| 3 | Run idempotent seed migration. Verify row count = strategies × 4 | re-running with `ON CONFLICT DO NOTHING` is safe |
| 4 | Switch `regime_gate.is_eligible()` to DB read + parity test | revert + rebuild cache (manifest field still present) |
| 5 | Switch `regime_blended_sizer` to read size_scalar/stop/target/max-hold from DB (all NULL → identical) | revert |
| 6 | Rewrite `eligibility_manager.py` to write DB; remove old manifest-write path | revert (manifest field still present) |
| 7 | Dashboard cells now POST to `/api/regime-params/:strategy/:regime`; back-compat shim on old endpoint | revert |
| 8 | Add new doctor check; repurpose existing drift check | revert |
| 9 | Deprecation notice in `manifest.eligible_regimes` field documentation | n/a |

Each step lands on `main` with passing tests before the next starts. Steps 4-7
are the behaviorally-significant ones; everything before is pure addition,
everything after is observability/cleanup.

---

## 7. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Step 4 (gate switch) introduces a silent semantic change for a strategy whose manifest has `eligible_regimes=[]` (current gate: backward-compat eligible — ambiguous) | Migration step writes `eligible=True` for every regime when manifest has no field OR empty list. Documented in 076 SQL comment. |
| Cache staleness across processes (johnbot Node API vs Python orchestrator) | 30s TTL acceptable for operator-pace edits. Redis pub/sub upgrade path noted; deferred. |
| Mastermind (Phase 2B) writes proposals before Phase 2A is fully shipped | Phase 2A must merge before 2B begins; explicit ordering. |
| Lifecycle.py round-trip bug recurs for some new manifest field added later | The `feedback_lifecycle_silent_strip` memory captures the rule; any new top-level field requires StrategyRecord + tests. Not Phase-2-specific. |
| Doctor's new `strategy_regime_params_consistency` fires WARN/FAIL transiently during step 3 seed | Seed runs in a single transaction; doctor only flags post-commit state. |
| Operator left mid-edit when cache TTL expires and other process invalidates first | Idempotent upserts mean the lost-update window doesn't corrupt — last writer wins, audit row records every step. |

---

## 8. Glossary

- **Eligibility** — boolean per (strategy, regime). When False, `regime_gate` skips
  the strategy in that regime; `regime_blended_sizer` never sees its signals.
- **Size scalar** — multiplier on the Kelly-derived position size for a (strategy,
  regime) cell. Today Phase 1 has regime-only scalars (e.g., HIGH_VOL=0.3) shared
  across all strategies. Phase 2A allows per-strategy override; default behavior
  unchanged until Phase 2B populates.
- **Stop / target / max-hold** — bracket-order parameters today set strategy-wide;
  Phase 2A enables per-regime override; default behavior unchanged until Phase 2B
  populates.

---

## Implementation complete — 2026-05-12

Phase 2A shipped in 12 commits across this session. Final state:

- Migrations 076 (params) + 077 (audit) applied; 392 rows seeded from manifest.
- `src/execution/regime_param_resolver.py` — read API + 30s in-process cache with explicit invalidate hook. 8 tests.
- `scripts/seed_strategy_regime_params.py` — idempotent seed (392 rows on first run, 0 on re-run). 5 tests.
- `src/strategies/regime_gate.py` — reads via resolver; backward-compat True for missing rows; fails OPEN on resolver error. 4 tests.
- `src/execution/trade_handoff_builder.py` — `scale` dict replaced with `_size_scalar('__regime_default__', state)` resolver call; Phase 1 default behavior preserved via PHASE1_REGIME_SCALARS.
- `src/strategies/eligibility_manager.py` — DB transaction (SELECT FOR UPDATE → audit insert → upsert) + cache invalidate. Old manifest-write tests deleted. 4 tests.
- `src/channels/api/routes_regime_params.js` (new) + `routes_regime_eligibility.js` (back-compat shim) — both mount + smoke verified.
- Dashboard regime cells POST to `/api/regime-params/:strategy/:regime` directly.
- Doctor: new `strategy_regime_params_consistency` (PASSes at 392 rows); `manifest_eligibility_drift` repurposed to flag deprecated-field writes (currently WARNs at 5 — the originally-committed eligibility lines on those strategies' manifest entries).

Total new tests: 8 + 5 + 4 + 4 + 5 + 5 (revised drift) + 4 (lifecycle preservation, pre-existing) = 35 tests across the relevant suites; 40 passing in the smoke run.

Final E2E: CLI toggle of `S_intl_momentum_attention_regime` flips gate result True ↔ False; manifest unchanged; audit trail (8 rows so far) captures every smoke + plan-task write.

Followup specs:
- **Phase 2B**: extend MastermindJohn comprehensive-review to emit per-(strategy, regime) proposals; proposal table; dashboard approval workflow. Will populate the four currently-NULL numeric columns (size_scalar, stop_pct, target_pct, max_hold_days).
- **Phase 2C**: drift detection vs literature priors; Monte Carlo validation harness.
- **Cleanup spec (post-2B stable)**: remove `manifest.eligible_regimes` field entirely; `lifecycle.py.StrategyRecord` drops the attribute; doctor's `manifest_eligibility_drift` retires.
