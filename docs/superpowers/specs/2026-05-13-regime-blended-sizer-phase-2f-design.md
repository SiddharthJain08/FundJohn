# Phase 2F — Mastermind Prompt Recalibration Loop

**Status:** Spec — 2026-05-13
**Prereq:** Phase 2D (calibration tracking) shipped 2026-05-12
**Out of scope:** intraday-path MC (Phase 2E shipped 2026-05-13), correlation-adjusted sizing (Phase 2G shipped 2026-05-12)

---

## 0. Honesty preamble (read before reviewing)

This phase ships infrastructure that **cannot fire its primary path until ~Nov 2026**. As of 2026-05-13, `mastermind_proposal_outcomes` is empty (zero decided proposals with ≥30d outcome windows). The bias detector needs ≥10 observations per confidence bucket to emit; that's months of weekly Mastermind runs away.

Why ship it now anyway:
1. **Wiring point in `comprehensive_review.js`** — every Saturday, Mastermind runs. Threading the addendum-injection into that prompt now means when the data arrives, no further code change is needed.
2. **Operator-approval workflow mirrors Phase 2B** — same approve/reject/modify lifecycle, dashboard surface, doctor check. Building it once amortizes.
3. **Manual-mode escape hatch** — operators can hand-author addenda (source=`operator`) immediately, even before the bias detector becomes useful. This is the real value-add today: an operator who notices "Mastermind keeps overweighting AI-themed tickers" can write a corrective addendum and have it injected into next Saturday's prompt without code change.

The spec is explicit about both modes (auto + operator) and the dormancy gate.

---

## 1. Why each piece

**Bias detector.** Phase 2D's `calibration_report()` surfaces per-bucket match rates. If Mastermind's 0.8-confidence bucket has 30 observations but the match rate is 0.55, that's a 25-point gap — Mastermind is overconfident in that bucket. The detector quantifies the bias and emits a candidate `addendum` text that, prepended to the next prompt, instructs Mastermind to discount confidence in the affected bucket.

**Addendum table + lifecycle.** `mastermind_prompt_addenda` is append-only. Status transitions: `pending` (auto-generated, awaits operator approve/reject) → `active` (injected into prompts) → `expired` (past `valid_until`) | `superseded` (newer addendum replaced it) | `rejected` (operator declined).

**Prompt injection.** `comprehensive_review.js` loads `active` addenda at run-start, prepends their text to the Opus system prompt, and records `addenda_ids_active` in the run metadata for audit.

**Dormancy gate.** The detector explicitly returns `INSUFFICIENT` until each bucket has `MIN_BUCKET_SAMPLES=10` observations with `|match_rate - bucket_midpoint| > BIAS_DELTA_THRESHOLD=0.15`. No noisy small-sample auto-emission.

---

## 2. Schema

### Migration 086 — `mastermind_prompt_addenda`

```sql
CREATE TABLE IF NOT EXISTS mastermind_prompt_addenda (
    id              BIGSERIAL    PRIMARY KEY,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source          TEXT         NOT NULL,  -- 'auto:bias_detector' | 'operator:<name>'
    triggered_by    JSONB,                  -- {bucket, count, match_rate, midpoint, delta} for auto
    addendum_text   TEXT         NOT NULL,  -- what gets prepended
    rationale       TEXT,                   -- operator-readable why
    valid_from      TIMESTAMPTZ,            -- NULL = effective immediately on approval
    valid_until     TIMESTAMPTZ,            -- NULL = no auto-expire
    status          TEXT         NOT NULL DEFAULT 'pending',
                                            -- 'pending'|'active'|'expired'|'superseded'|'rejected'
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_reason TEXT,
    supersedes_id   BIGINT REFERENCES mastermind_prompt_addenda(id)
);

CREATE INDEX IF NOT EXISTS idx_mpa_status_created
    ON mastermind_prompt_addenda (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mpa_active_window
    ON mastermind_prompt_addenda (status, valid_until)
 WHERE status = 'active';
```

---

## 3. Module — `src/agent/mastermind_recalibration.py`

```python
MIN_BUCKET_SAMPLES = 10
BIAS_DELTA_THRESHOLD = 0.15   # |match_rate - midpoint| > 0.15 → biased

def detect_bias() -> list[dict]
    # Reads calibration_report().buckets, returns one entry per biased bucket
    # ({bucket_label, count, match_rate, midpoint, delta, direction})

def generate_addendum(bias_entry: dict) -> str
    # Templated text: "Recent calibration: your <bucket> confidence calls
    # matched <rate>% of the time vs <midpoint>% expected. Discount accordingly."

def emit_auto_addenda(dry_run: bool = False) -> list[int]
    # For each biased bucket: insert pending addendum (source='auto:bias_detector')

def approve_addendum(id: int, decided_by: str, reason: str = '') -> dict
def reject_addendum(id: int, decided_by: str, reason: str = '') -> dict
def expire_addendum(id: int, decided_by: str, reason: str = '') -> dict
def create_operator_addendum(text: str, rationale: str, decided_by: str,
                              valid_until: Optional[datetime] = None) -> int
    # Pre-approved, status='active' immediately

def get_active_addenda() -> list[dict]
    # Read API used by comprehensive_review.js; also handles auto-expire
    # of past-valid_until rows.
```

CLI: `python3 -m agent.mastermind_recalibration --detect | --emit | --list | --approve N | --reject N | --expire N | --add "TEXT"`

---

## 4. Wiring — `src/agent/curators/comprehensive_review.js`

At the top of `_runMastermindReview()` (or wherever the Opus call is assembled), before the strategy memo template is constructed:

```javascript
const { spawnSync } = require('child_process');
function loadActiveAddenda() {
  const p = spawnSync(PYTHON, ['-m', 'agent.mastermind_recalibration', '--list-active'],
                      { cwd: OPENCLAW_DIR, env: process.env });
  if (p.status !== 0) return [];
  try { return JSON.parse(p.stdout.toString()).addenda || []; }
  catch (_) { return []; }
}
const addenda = loadActiveAddenda();
const calibrationPrefix = addenda.length
  ? `## Calibration addenda (operator-approved):\n${addenda.map(a => `- ${a.addendum_text}`).join('\n')}\n\n`
  : '';
const fullPrompt = calibrationPrefix + STRATEGY_REVIEW_TEMPLATE.replace(...);
```

Each Opus invocation also logs `addenda_ids_active` to `strategy_memos.metadata` for audit traceability.

---

## 5. API — `src/channels/api/routes_recalibration.js`

- `GET /api/recalibration/addenda?status=active|pending|all` — list
- `POST /api/recalibration/addenda/:id/approve` — body `{decided_by, reason?}`
- `POST /api/recalibration/addenda/:id/reject` — body `{decided_by, reason?}`
- `POST /api/recalibration/addenda/:id/expire` — body `{decided_by, reason?}`
- `POST /api/recalibration/addenda` — operator-authored. body `{addendum_text, rationale, decided_by, valid_until?}`
- `POST /api/recalibration/detect` — run bias detector, return candidate bias entries (no insert)
- `POST /api/recalibration/emit` — run detector + insert pending rows for each biased bucket

---

## 6. Doctor

### `mastermind_addendum_health`

- **PASS**: no problems
- **WARN**: 1-2 expired rows still tagged `active` (auto-expire didn't fire)
- **FAIL**: ≥3 expired-still-active OR any pending addendum aged >30d (operator-decision rot)

---

## 7. Tests

| File | Coverage |
|---|---|
| `test_mastermind_recalibration.py` | detect_bias on synthetic buckets (insufficient/biased/clean), generate_addendum text format, emit_auto_addenda inserts correctly + idempotent (same bucket twice = supersedes), approve/reject/expire transitions, create_operator_addendum is immediately active, get_active_addenda filters by valid_from/until |
| `test_doctor_mastermind_addendum_health.py` | PASS/WARN/FAIL tiers |
| `test_comprehensive_review_prompt_injection.js` | active addenda are prepended; empty list leaves prompt unchanged |

---

## 8. Rollout

1. Migration 086.
2. Module + tests + CLI.
3. API endpoint file + server.js wiring.
4. `comprehensive_review.js` injection point.
5. Doctor check.
6. Smoke run with zero outcomes — must NOT emit (INSUFFICIENT).
7. Smoke run with synthetic biased bucket data — must emit pending row.
8. Spec footer + memory update.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Auto-emission accidentally fires before sufficient data | Hard gate at `MIN_BUCKET_SAMPLES=10`; detector returns INSUFFICIENT |
| Addendum text introduces prompt-injection vector | All addenda require operator approval before status flips to `active`; operator-authored ones are pre-approved by the writer. Text is plain-text only, no template rendering. |
| Multiple competing addenda for same bucket clutter the prompt | `supersedes_id` chain; bias detector marks old auto-addendum as `superseded` when emitting a new one for the same bucket. |
| Prompt-injection wiring fails silently | `comprehensive_review.js` logs the addenda IDs actually injected; doctor `mastermind_addendum_health` cross-checks. Failure mode is "addenda not applied" not "wrong answer" — Mastermind still runs. |
| Addendum text grows over time and chews 1M-ctx tokens | Operator-driven expiry; doctor flags pending-too-long; soft max of 5 active addenda (warn beyond that). |

---

## 9.5 Deferred from this rollout

- **Per-memo `addenda_ids_active` write to `strategy_memos.metadata`.**
  The 2F run-level summary returns `addendaApplied: [id, ...]` and the
  `mastermind_addendum_health` doctor check covers lifecycle hygiene, but
  the per-strategy-memo audit row that ties memo `X` to addenda `[1, 7]`
  is not yet wired. Acceptable for the operator-approval workflow today
  (active set is small; ambiguity is low). Worth doing if/when there are
  ≥3 concurrent active addenda and a memo's outputs surprise the operator.

## 10. Out of scope (future)

- Per-strategy addenda (current scope is global to Mastermind's Saturday run)
- Automatic addendum-text regeneration via LLM (current scope is templated)
- A/B testing of addendum effectiveness (needs split testing infrastructure)
- Sentiment/audit log review of addenda — could feed back into the detector
