# Regime-Blended Sizer — Phase 2B: Mastermind Proposer + Operator Approval

**Status:** designed 2026-05-12; implementation in same session.

**Scope:** extend MastermindJohn comprehensive-review (Sat 18:00 ET) to emit per-(strategy, regime) parameter proposals; persist to a new proposals table; add operator approve/reject/modify workflow surfacing in the dashboard. Builds directly on Phase 2A.

**Out of scope** (Phase 2C / cleanup specs):
- Drift detection vs literature priors
- Monte Carlo validation harness
- Auto-approval rules + confidence-threshold automation
- Manifest.eligible_regimes field deletion

---

## 1. Why Phase 2B

Phase 2A landed the per-(strategy, regime) parameter infrastructure: a DB
table `strategy_regime_params` with four nullable columns (`size_scalar`,
`stop_pct`, `target_pct`, `max_hold_days`) per (strategy, regime), wired
to the gate and the sizer. All four columns are NULL today; behavior is
identical to Phase 1.

Phase 2B fills those columns based on **evidence**: MastermindJohn already
runs a weekly per-strategy lifetime review (Saturday 18:00 ET) and writes
strategy-wide tuning recommendations (`size_pct_delta`, `stop_delta_pct`,
`target_delta_pct`, `hold_days_delta`) to `strategy_memos`. 2B extends
that same prompt to also emit a `regime_recommendations` array — one
entry per regime the strategy has materially traded in — and routes those
entries through an operator approval queue before they hit
`strategy_regime_params`.

**Key constraint:** the operator approves every proposal. No
auto-application. Phase 2B is "ML-assisted operator decision support,"
not "AI-driven parameter changes."

---

## 2. Schema

### Migration 078 — `strategy_regime_param_proposals`

```sql
CREATE TABLE IF NOT EXISTS strategy_regime_param_proposals (
    id                  BIGSERIAL    PRIMARY KEY,
    proposed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    proposer            TEXT         NOT NULL,    -- 'mastermind:<run_id>' | 'operator:<name>'
    strategy_id         TEXT         NOT NULL,
    regime_state        TEXT         NOT NULL,    -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    current_row         JSONB,                    -- snapshot of strategy_regime_params at proposal time
    proposed_eligible        BOOLEAN,
    proposed_size_scalar     NUMERIC,
    proposed_stop_pct        NUMERIC,
    proposed_target_pct      NUMERIC,
    proposed_max_hold_days   INTEGER,
    confidence          NUMERIC,                  -- 0.0 - 1.0
    reasoning           TEXT,                     -- short justification from Mastermind
    memo_id             UUID,                     -- FK to strategy_memos.id; NULL for operator-created proposals
    status              TEXT         NOT NULL DEFAULT 'pending',
                                                  -- 'pending' | 'approved' | 'rejected' | 'modified' | 'superseded'
    decided_at          TIMESTAMPTZ,
    decided_by          TEXT,                     -- 'operator:<name>'
    decision_reason     TEXT,
    applied_row         JSONB                     -- snapshot of what was written to strategy_regime_params on approve/modify
);

CREATE INDEX IF NOT EXISTS idx_srpp_status
    ON strategy_regime_param_proposals (status, proposed_at DESC);

CREATE INDEX IF NOT EXISTS idx_srpp_strategy_regime
    ON strategy_regime_param_proposals (strategy_id, regime_state, proposed_at DESC);
```

**Schema choices:**
- One row per (strategy, regime) per proposal cycle. A weekly Mastermind run might emit 0–4 rows per strategy depending on which regimes have material data.
- Proposal **doesn't** mandate setting all four params — Mastermind can propose just eligibility, just size_scalar, or any subset. NULL means "no change recommended for this column" (operator can still edit on the way through).
- `status='superseded'`: when a new Mastermind run produces a proposal for the same (strategy, regime) that overlaps a still-pending one, the older one auto-supersedes. Prevents stale proposals piling up.
- `memo_id` FK back to the strategy_memos row that produced it, so the operator can read the full Opus reasoning when approving.
- `applied_row` captures the actual row state that landed in `strategy_regime_params` after approval (which may differ from `proposed_*` if operator modified). Audit trail.

---

## 3. Mastermind extension

**File:** `src/agent/curators/comprehensive_review.js`

**Existing flow:** per live strategy, builds a trade pack (lifetime signals + pnl + counterfactuals + 30-day vetos), prompts Opus with a memo template, parses `lifetime_summary / parameter_analysis / recommendations` JSON, writes `strategy_memos`.

**Phase 2B changes:**
1. **Trade pack enrichment**: aggregate the trade pack BY REGIME — for each (strategy, regime), produce a sub-block with `trade_count`, `closed_count`, `win_rate`, `avg_realized_pct`, `worst_trade_pct`, plus a counterfactual for `size_scalar=0.5x` and `size_scalar=1.5x` per regime.
2. **Prompt extension**: a new section in the memo template asking Opus to emit:
   ```jsonc
   "regime_recommendations": [
     {
       "regime_state": "LOW_VOL",
       "eligible":           true,                // or false to propose trim; NULL to leave unchanged
       "size_scalar":        0.7,                 // NULL → no change recommended
       "stop_pct":           null,                // NULL → no change
       "target_pct":         null,
       "max_hold_days":      null,
       "confidence":         0.75,
       "reasoning_one_line": "20 trades, +0.6% avg, win 65%; current 1.0x scalar over-allocates given drawdown profile — recommend 0.7x"
     },
     // ... up to 4 entries; skip a regime if no actionable data
   ]
   ```
3. **Persistence**: after writing `strategy_memos`, also write one row to `strategy_regime_param_proposals` per entry in `regime_recommendations`. `memo_id = <memo row's id>`. Set status to `'pending'`. Older still-`pending` proposals for the same (strategy_id, regime_state) get marked `status='superseded'` in the same transaction.
4. **Discord notification**: append a one-line summary to the strategy's memo markdown ("X regime proposals awaiting approval — see /api/regime-proposals") so operators discover proposals via the existing #strategy-memos channel.

**Prompt evolution risk**: Opus might produce malformed `regime_recommendations` (extra fields, wrong types, illegal regime names). Defensive parser drops malformed entries and logs them; never blocks the rest of the memo from landing.

---

## 4. Operator surface

### Backend module — `src/strategies/proposal_manager.py`

Mirrors `eligibility_manager.py` shape:

```python
def list_proposals(*, status='pending', limit=50) -> list[dict]: ...
def approve(*, proposal_id, actor, reason='', source='dashboard') -> dict: ...
def reject(*, proposal_id, actor, reason='', source='dashboard') -> dict: ...
def modify(*, proposal_id, actor, overrides: dict, reason, source='dashboard') -> dict: ...
```

- `approve`: opens a Postgres transaction, locks the proposal row FOR UPDATE, validates it's still pending, calls `eligibility_manager.set_params(...)` with the proposed values, captures the resulting row state into `applied_row`, marks the proposal `status='approved'` with `decided_at/decided_by/decision_reason`. Commits.
- `reject`: marks the proposal `status='rejected'` with a reason; doesn't touch `strategy_regime_params`.
- `modify`: takes an `overrides` dict; merged onto the `proposed_*` columns; otherwise same path as `approve`. Final marker is `status='modified'`.

CLI exposes `python3 -m strategies.proposal_manager --list / --approve <id> / --reject <id> --reason '...' / --modify <id> --size 0.6 ...`.

### API — `src/channels/api/routes_regime_proposals.js`

- `GET /api/regime-proposals?status=pending&limit=50` → list, joined with strategy_memos.markdown_body for context
- `POST /api/regime-proposals/:id/approve` body `{actor, reason}`
- `POST /api/regime-proposals/:id/reject` body `{actor, reason}`
- `POST /api/regime-proposals/:id/modify` body `{actor, reason, overrides: {...}}`

All writes shell out to the Python `proposal_manager` (same pattern as Phase 2A).

### Dashboard — strategies-page extension

The strategies page already has clickable regime cells (Phase 2A). 2B adds:
- A small badge on cells with pending proposals (e.g., a yellow dot + count).
- A collapsible "Pending proposals" section at the top of the page listing each strategy's pending proposals with: current state, proposed state, confidence, one-line reasoning, "View memo" link, Approve / Reject / Modify buttons.
- Approve/Reject = direct POST. Modify = inline edit form populated with proposed values; operator can adjust then submit.
- After any decision: page refreshes; the proposal disappears from the panel; the cell badge clears.

---

## 5. Doctor check

**New check:** `regime_proposals_backlog`

- PASS: 0 pending proposals older than 14 days
- WARN: 1–9 pending proposals older than 14 days
- FAIL: ≥10 pending OR any pending older than 30 days

The goal isn't "no pending proposals exist" — those are normal between Saturday runs and the next operator review window. The goal is **operator review hygiene**: proposals shouldn't stagnate.

---

## 6. Testing

| File | Coverage | ~Tests |
|---|---|---|
| `tests/test_proposal_manager.py` | list / approve / reject / modify; FOR UPDATE on stale proposal raises; approve calls eligibility_manager.set_params with right args; superseded skip | 8 |
| `tests/test_comprehensive_review_regime_proposals.py` | regime breakdown of trade pack; prompt template includes new section; parser drops malformed entries; supersede-old-pending in transaction | 5 |
| `tests/test_doctor_regime_proposals_backlog.py` | 0 pending → PASS; 1+ in window → PASS; 1+ aged >14d → WARN; ≥10 aged → FAIL; DB error → WARN | 5 |

**Total new tests: ~18.** No integration smoke against a live Mastermind run in this session (would need a real Saturday cycle); end-to-end exercised by stubbing the Opus call with canned `regime_recommendations` JSON and confirming proposals land in DB.

---

## 7. Rollout sequence

| # | Step | Risk |
|---|---|---|
| 1 | Migration 078 (DDL) | none |
| 2 | `proposal_manager.py` + tests | none (no consumer yet) |
| 3 | `comprehensive_review.js` extension + counterfactual-by-regime helper | parser robustness key |
| 4 | API endpoints + back-end smoke (stubbed proposals) | none |
| 5 | Dashboard pending-proposals panel + cell badge | UI only |
| 6 | CLI for operator approval | none |
| 7 | Doctor `regime_proposals_backlog` check | none |
| 8 | End-to-end smoke with canned proposal payload; clean up smoke proposals | none |
| 9 | Spec footer + runbook update + memory | none |

Each step lands on `main` independently. The full pipeline only "activates" once the next Saturday cycle runs comprehensive-review with the new prompt (~7 days from any 2B merge), at which point real proposals start showing up. Operator can manually seed test proposals via the CLI before that.

---

## 8. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Opus produces malformed `regime_recommendations` array | Defensive parser drops malformed entries + logs; rest of memo unaffected |
| Stale pending proposals pile up | `regime_proposals_backlog` doctor check + auto-supersede on new run for same (strategy, regime) |
| Two operators approve the same proposal simultaneously | SELECT FOR UPDATE in approve transaction; second arriver gets "already decided" error |
| Approved proposal references stale current_row (operator made an out-of-band change since proposal) | `approve` uses CURRENT row from strategy_regime_params (not the proposal's snapshot); proposal's `current_row` is informational only |
| Operator rejects without a reason | API accepts empty reason but logs a WARN; CLI/dashboard prompt for one |
| Mastermind cost: per-strategy prompts grow by ~30% for the regime data | Acceptable — cost is fixed weekly, not per-cycle; budget already accommodates Opus comprehensive-review |

---

## Implementation complete — 2026-05-12

Phase 2B shipped in 8 commits same session as Phase 2A:

- `b798554` Migration 078 (`strategy_regime_param_proposals`)
- `254de99` `proposal_manager.py` — approve/reject/modify/insert/supersede (8 tests)
- `8af1dc4` `comprehensive_review.js` extension: prompt template adds `regime_recommendations`, defensive parser, auto-supersede on new proposal for same (strategy, regime), one proposal per valid entry. Memo markdown auto-references the proposal queue.
- `2ff6b64` `/api/regime-proposals` endpoints (GET list, POST approve/reject/modify)
- `adfd115` Dashboard pending-proposals panel above the Active Stack; Approve/Reject buttons; auto-hides when empty
- `81787ba` Doctor `regime_proposals_backlog` check (6 tests; PASS / WARN / FAIL thresholds at 14d / 30d / count=10)
- This commit (docs)

**E2E smoke verified 2026-05-12T21:10 UTC:**
- Manually-seeded proposal for `momentum_12_1 / HIGH_VOL` (eligible→true, size_scalar→0.35)
- `POST /api/regime-proposals/1/approve` ran the full chain: SELECT FOR UPDATE → eligibility_manager.set_params → mark approved with applied_row snapshot
- `strategy_regime_params` reflected: eligible=true, size_scalar=0.35, set_by='operator:plan-task7-smoke'
- `regime_gate.is_eligible('momentum_12_1', 'HIGH_VOL')` → True; resolver `size_scalar` → 0.35
- Audit chain: proposal status flipped to 'approved', applied_row JSONB persisted

**41 plan tests pass; 20 doctor checks (19 pass + 1 expected WARN on manifest_eligibility_drift).**

**Operational note:** No real Mastermind run has fired with the new prompt yet — that happens next Saturday at 18:00 ET. Operator can seed test proposals via `python3 -m strategies.proposal_manager` or via `proposal_manager.insert_proposal()` for ad-hoc reviews.

**Followups (Phase 2C / cleanup):**
- Drift detection: live regime perf vs Mastermind-set baseline; alert on divergence.
- Literature priors: optional `expected_*` columns on `strategy_regime_params` that proposer can target; drift alarm if live diverges.
- Auto-approval rules: confidence threshold + bounded delta; bypass operator for clearly-good proposals.
- `manifest.eligible_regimes` field deletion (post-2A stable).
- `eligibility_manager.set_params` sentinel to explicitly reset a populated column back to NULL (needed if operator wants to "undo" a Phase 2B-approved scalar back to Phase 1 default).
