# Regime-Blended Sizer — Phase 2C: Priors, Drift Detection, Auto-Approval, Cleanup

**Status:** designed + shipping 2026-05-12 in same session as 2A+2B.

**Scope:** literature priors + live drift detection + bounded auto-approval rules for high-confidence proposals + cleanup (manifest.eligible_regimes deletion + set_params NULL-reset sentinel).

**Deliberately deferred to Phase 2D:** Monte Carlo validation harness. Bootstrap CI for size_scalar changes is a small subset of "proper MC" and doing the proper version requires intraday paths for stop/target simulation. Out of scope here; gets a separate research-quality spec.

---

## 1. Why Phase 2C

Phase 2B gives MastermindJohn an approval queue. But:
- **No baseline to drift against**: when live behavior diverges from the expected Sharpe / win-rate, nothing surfaces it. Operators only see proposals at Mastermind cadence (weekly).
- **No literature anchor**: the only "expected performance" is whatever Opus says in a memo. Research papers cite expected Sharpe in specific regimes; capturing that explicitly enables drift comparison.
- **Every proposal needs operator touch**: a perfectly-mechanical proposal ("strategy fired 200 profitable trades in HIGH_VOL with confidence 0.99 — approve eligibility expansion") still requires a click. Wastes operator attention.
- **Cleanup overdue**: `manifest.eligible_regimes` is still on disk for 5 strategies. The `set_params` API can't reset a populated NUMERIC back to NULL.

---

## 2. Schema

### Migration 079 — `strategy_regime_priors`

```sql
CREATE TABLE IF NOT EXISTS strategy_regime_priors (
    strategy_id        TEXT         NOT NULL,
    regime_state       TEXT         NOT NULL,   -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    expected_sharpe    NUMERIC,                  -- annualized, post-fee
    expected_win_rate  NUMERIC,                  -- 0.0 - 1.0
    expected_avg_pnl_pct NUMERIC,
    source             TEXT         NOT NULL,   -- e.g. 'Asness 2013', 'Mastermind:2026-05-12'
    confidence         NUMERIC,                  -- 0.0 - 1.0
    notes              TEXT,
    set_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    set_by             TEXT         NOT NULL,
    PRIMARY KEY (strategy_id, regime_state)
);
```

**Schema choices:**
- Sparse table: only populated for (strategy, regime) pairs where research or operator has set a prior. Unpopulated rows mean "no baseline — drift compares against applied params history instead".
- One source per row. Multiple priors per (strategy, regime) over time → use a single audit-trail-via-changes pattern (migration 080).

### Migration 080 — `strategy_regime_priors_changes` (audit)

```sql
CREATE TABLE IF NOT EXISTS strategy_regime_priors_changes (
    id              BIGSERIAL    PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,
    before_row      JSONB,
    after_row       JSONB        NOT NULL,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_srpr_changes_strategy_time
    ON strategy_regime_priors_changes (strategy_id, regime_state, changed_at DESC);
```

---

## 3. Drift detector

**File:** `src/metrics/regime_param_drift.py`

For each (strategy, regime) row in `strategy_regime_live_pnl_rollup` (latest snapshot), compare against:

1. **Prior baseline** — `strategy_regime_priors.expected_sharpe / win_rate / avg_pnl_pct` if present.
2. **Approved baseline** — the most recent `strategy_regime_param_proposals.applied_row` with non-NULL numeric proposals (if any). Captures "what we approved as the right setting" — drift here means the setting we picked is no longer producing what we expected.

**Drift signal per (strategy, regime):**

```python
{
    'strategy_id': 's1',
    'regime_state': 'LOW_VOL',
    'live_sharpe': 1.2,
    'live_win_rate': 0.58,
    'live_avg_pnl_pct': 0.012,
    'live_trade_count': 45,        # window: 90d
    'prior_sharpe': 1.5,            # may be None
    'prior_source': 'Asness 2013',
    'sharpe_delta': -0.3,           # live - prior
    'win_rate_delta': -0.07,
    'severity': 'WARN',             # 'OK' | 'WARN' | 'FAIL'
    'reason': 'Sharpe 1.2 vs prior 1.5 — 20% below expectation over 45 trades'
}
```

**Severity rules:**
- `OK`: |sharpe_delta| < 0.5 AND |win_rate_delta| < 0.10 AND trade_count >= 10
- `WARN`: |sharpe_delta| in [0.5, 1.0] OR |win_rate_delta| in [0.10, 0.20]
- `FAIL`: |sharpe_delta| >= 1.0 OR |win_rate_delta| >= 0.20
- **Insufficient data** (`trade_count < 10`): never flagged, returns `'INSUFFICIENT'` (informational, not WARN)

Public API:

```python
def compute_drift(strategy_id=None, regime_state=None) -> list[dict]
def latest_drift_summary() -> dict   # {ok: N, warn: N, fail: N, insufficient: N}
```

---

## 4. Auto-approval rules

**File extension:** `src/strategies/proposal_manager.py`

Add `auto_approve(proposal_id)` that:
1. Loads the proposal.
2. Checks bounded-delta config (env vars):
   - `OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE` (default 0.85)
   - `OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA` (default 0.20 absolute) — `|proposed_size - current_size|`
   - `OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_STOP_DELTA` (default 0.01 absolute) — i.e., 1% stop change
3. If all rails are met, marks `proposer='auto-approval'` decision and routes through `approve(...)`. Logs the decision with full context for audit.
4. Otherwise: no-op (proposal stays pending).

**Disabled by default.** `OPENCLAW_PROPOSAL_AUTOAPPROVE=1` enables. When disabled, the function is a no-op even if invoked.

**Where it's called:** new function in `comprehensive_review.js` AFTER all proposals are inserted, iterating each fresh proposal ID and calling `auto_approve`. Mastermind effectively becomes "propose + auto-approve trivial ones; defer the rest to operator."

---

## 5. Cleanup deliverables

### 5a. `eligibility_manager.set_params` NULL-reset sentinel

Today, passing `size_scalar=None` to `set_params` means "keep existing". To explicitly reset a populated column back to NULL, callers pass a sentinel: a string literal `'__NULL__'`. The function detects the sentinel and writes a real SQL NULL. Documented in the docstring.

### 5b. Manifest `eligible_regimes` field removal

A new script `scripts/cleanup_manifest_eligibility_field.py`:
- Reads manifest.json.
- For each strategy with `eligible_regimes` field, removes the field. Verifies the (strategy_id, regime) rows already exist in `strategy_regime_params` (which seed migration guarantees).
- Atomic write via lifecycle's manifest_lock.

Run once during rollout. After: `manifest_eligibility_drift` doctor check goes PASS.

### 5c. Lifecycle.py drops the field

`StrategyRecord.eligible_regimes` attribute and its emission in `to_dict()` removed; `from_manifest()` no longer reads it. Backward compat preserved by the resolver's fall-back-True semantics for missing DB rows.

---

## 6. Operator surface

### Dashboard — drift indicator
- New badge on regime cells: small dot (yellow=WARN, red=FAIL) when `compute_drift(strategy, regime)` returns non-OK severity.
- Tooltip extends to include drift detail.

### Dashboard — priors panel (collapsible)
- Optional small panel listing strategies that DO have priors, with the live-vs-prior comparison.
- Hidden by default (only relevant when priors are populated).

### CLI — priors manager
`src/strategies/priors_manager.py`:
- `--list` / `--set <strategy> <regime> --sharpe N --win-rate N --source 'paper'`
- `--audit`

### API — drift endpoint
- `GET /api/regime-drift?strategy=<id>&regime=<regime>` → drift signal(s) for filtering
- `GET /api/regime-priors` → list priors
- `POST /api/regime-priors/:strategy/:regime` → upsert prior

---

## 7. Doctor check

`regime_param_drift_alerts`:
- PASS: no FAIL-severity drift signals; ≤2 WARN
- WARN: 3-9 WARN-severity drift signals
- FAIL: any FAIL-severity drift signal OR ≥10 WARN

---

## 8. Testing

| File | Coverage | ~Tests |
|---|---|---|
| `tests/test_regime_param_drift.py` | severity thresholds at boundaries; insufficient-data path; missing prior falls through to applied-baseline; latest_drift_summary aggregation | 6 |
| `tests/test_priors_manager.py` | upsert + audit row written; reject invalid regime; --list / --audit smoke | 5 |
| `tests/test_proposal_auto_approval.py` | env disabled → no-op; confidence below threshold → no-op; size delta outside band → no-op; happy path → approve called | 5 |
| `tests/test_eligibility_manager_null_sentinel.py` | `'__NULL__'` sentinel resets column; other kwargs unaffected | 3 |
| `tests/test_doctor_regime_param_drift_alerts.py` | 0 alerts → PASS; few WARN → PASS / WARN bands; FAIL severity → FAIL; many WARN → FAIL; DB error → WARN | 5 |
| `tests/test_lifecycle_eligible_regimes_field_removed.py` | new manifest with no field round-trips; old manifest with field round-trips without preserving | 2 |

**Total new tests: ~26.**

---

## 9. Rollout sequence

| # | Step | Risk |
|---|---|---|
| 1 | Migrations 079 + 080 | none |
| 2 | `priors_manager.py` + tests + API + CLI | none (no consumers yet) |
| 3 | `regime_param_drift.py` + tests | none |
| 4 | `set_params` NULL-sentinel + tests | none (additive) |
| 5 | `proposal_manager.auto_approve` + tests; wire into Mastermind | env-gated; default off |
| 6 | Doctor `regime_param_drift_alerts` + tests | none |
| 7 | Dashboard: drift badges + (collapsible) priors panel | UI only |
| 8 | `cleanup_manifest_eligibility_field.py` script — run once | manifest write under lock |
| 9 | Lifecycle.py: drop `eligible_regimes` attribute | the field is no longer authoritative; deletion is the cleanup |
| 10 | Spec footer + runbook section + memory | none |

---

## 10. Out of scope (Phase 2D)

- Monte Carlo validation harness (bootstrap CI for size_scalar; intraday path simulation for stop/target).
- Confidence calibration for Mastermind: compare Mastermind's stated confidence on past proposals against operator decisions + live outcomes; recalibrate prompt.
- Cross-strategy correlation analysis: when two strategies have overlapping signals in the same regime, the per-strategy sizer doesn't account for portfolio-level correlation.
