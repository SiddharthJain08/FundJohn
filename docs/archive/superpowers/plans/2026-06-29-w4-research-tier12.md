# W4 Research Tier-1+2 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Unblock the weekly research pipeline (the missing `process.exit(0)` that's hung the finisher + offlined the code-review for 2 weeks) and fix three observability defects (status mislabel, inverted funnel metric, dead dashboard tile).

**Architecture:** Four independent path-scoped fixes. C1 adds the process-exit (matches the file's own early-exit pattern). C2/C4 add a tiny pure mapping fn + a one-time idempotent DB backfill each (rolled-back temp-table tests, mirror `tests/test_migration_139.py`). C3 re-sources a dashboard query. systemd-unit changes (code-review split, ExecStartPre, zombie reap) are deploy-gated config, NOT in this plan.

**Tech Stack:** Node.js (curators, routes), Python 3 (backfill scripts + pytest), psycopg2, systemd. Tests: `node tests/*.js` / `node --check`; `python3 -m pytest`.

## Global Constraints
- PATH-SCOPED commits ONLY. Never `git add -A`/`.`. The live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `src/strategies/registry.py`, untracked `src/strategies/implementations/S_*`) — stage only each task's files explicitly + abort guard; verify the staged set. Never `git reset --hard`/`clean`/blind `checkout`.
- Do NOT restart any service, do NOT `git push`, do NOT apply backfills to the live DB — those are operator-gated deploy steps. Backfill scripts must be APPLY-only when explicitly run; their TESTS run on a rolled-back temp table and never touch live data.
- `research_candidates.status` + `paper_gate_decisions` are research-pipeline state, NOT master data (the NEVER-DELETE invariant covers prices/options/financials/macro/insider/earnings/prices_30m/historical_regimes/crypto_bars_1h + execution_signals/signal_pnl/alpaca_submissions/data_coverage/data_columns). UPDATE permitted; idempotent; deploy-gated.
- Commit footer EVERY commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from /root/openclaw.

---

### Task C1: finisher + recovery missing process.exit(0) (W4-1, master fix)

**Files:**
- Modify: `src/agent/curators/saturday_brain_finisher.js:339` (after `log('Finisher complete.');`)
- Modify: `src/agent/curators/saturday_brain_recovery.js` (after its `log('Recovery complete.')`, ~line 288)

**Interfaces — Produces:** nothing importable; a behavior fix (clean process exit).

- [ ] **Step 1: Confirm the bug + the file's own pattern.** Grep shows `saturday_brain_finisher.js` already calls bare `process.exit(0)` at its early-exits (lines 170, 185, 213) but NOT on the happy path (line 339, after `log('Finisher complete.')`). The pg pool (`_query._pool`, line 51) + the ResearchOrchestrator's ioredis hold the event loop open → the process hangs → the `Type=oneshot` unit times out at 4h. Same bug class fixed in `run_mastermind.js` (line 284 `await pool.end()` + 393 `.then(() => process.exit(0))`).

- [ ] **Step 2: Implement — finisher.** In `saturday_brain_finisher.js`, change the end of `main()`:
```js
  log('Finisher complete.');
  process.exit(0);
```
(Add the `process.exit(0);` line immediately after the existing `log('Finisher complete.');` at line 339 — matching the bare-exit pattern at lines 170/185/213. `main().catch(...)` at line 342 already handles the error path with `process.exit(1)`.)

- [ ] **Step 3: Implement — recovery.** In `saturday_brain_recovery.js`, after its happy-path `log('Recovery complete.')` (~line 288, before the function returns), add:
```js
  process.exit(0);
```
(Confirm the `.catch` already does `process.exit(1)` on error; add the success-path exit only.)

- [ ] **Step 4: Verify.**
```bash
node --check src/agent/curators/saturday_brain_finisher.js && node --check src/agent/curators/saturday_brain_recovery.js
grep -n "process.exit(0)" src/agent/curators/saturday_brain_finisher.js   # now includes the line after 'Finisher complete.'
grep -n "Finisher complete\|Recovery complete" src/agent/curators/saturday_brain_finisher.js src/agent/curators/saturday_brain_recovery.js
```
(No unit test — a `process.exit` on the happy path of a long oneshot main() isn't unit-testable; verified by inspection + the grep + the matched early-exit pattern. Note in the report.)

- [ ] **Step 5: Commit (path-scoped).**
```bash
cd /root/openclaw && git add src/agent/curators/saturday_brain_finisher.js src/agent/curators/saturday_brain_recovery.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/agent/curators/saturday_brain_finisher.js src/agent/curators/saturday_brain_recovery.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "fix(research): finisher+recovery exit(0) on happy path — unblock weekly code lane (W4-1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task C2: funnel curator-metric inversion (W4-3)

**Files:**
- Modify: `src/agent/curators/mastermind.js:1006` (the outcome ternary)
- Create: `scripts/backfill_curator_gate_decisions.py` (one-time, apply-only)
- Test: `tests/test_backfill_curator_gate_decisions.py`

**Interfaces — Produces:** corrected `paper_gate_decisions.outcome` for `implementable_candidate`.

- [ ] **Step 1: Fix the ternary.** `mastermind.js:1006` currently: `const outcome = r.predicted_bucket === 'high' ? 'pass' : 'reject';`. The file already defines `HIGH_BUCKETS = new Set(['high', 'implementable_candidate'])` at line 106 (the promoted buckets). Change line 1006 to:
```js
    const outcome = HIGH_BUCKETS.has(r.predicted_bucket) ? 'pass' : 'reject';
```
Verify `node --check src/agent/curators/mastermind.js` and `grep -n "HIGH_BUCKETS.has(r.predicted_bucket)" src/agent/curators/mastermind.js`.

- [ ] **Step 2: Write the failing backfill test** — `tests/test_backfill_curator_gate_decisions.py` (rolled-back temp table, mirror `tests/test_migration_139.py`):
```python
# tests/test_backfill_curator_gate_decisions.py — the backfill flips historical curator
# gate_decisions for implementable_candidate papers from 'reject' to 'pass'. Runs on a TEMP
# table inside a rolled-back txn — never touches live paper_gate_decisions.
import os, pytest
try:
    import psycopg2
except ImportError:
    psycopg2 = None
from importlib import import_module

BACKFILL_SQL = (
    "UPDATE {t} SET outcome='pass' "
    "WHERE gate_name='curator' AND outcome='reject' AND predicted_bucket='implementable_candidate'"
)

@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_backfill_flips_only_implementable_reject_rows():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    conn = psycopg2.connect(dsn); conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("CREATE TEMP TABLE _pgd (gate_name text, outcome text, predicted_bucket text) ON COMMIT DROP")
        cur.execute("INSERT INTO _pgd VALUES "
                    "('curator','reject','implementable_candidate'),"   # -> flip to pass
                    "('curator','reject','low'),"                        # stays reject
                    "('curator','pass','high'),"                         # untouched
                    "('hunter','reject','implementable_candidate')")     # not curator gate -> untouched
        cur.execute(BACKFILL_SQL.format(t="_pgd"))
        cur.execute("SELECT outcome,predicted_bucket,gate_name FROM _pgd ORDER BY 1,2,3")
        got = cur.fetchall()
        assert ('pass','implementable_candidate','curator') in got
        assert ('reject','low','curator') in got
        assert ('pass','high','curator') in got
        assert ('reject','implementable_candidate','hunter') in got   # hunter gate untouched
    finally:
        conn.rollback(); conn.close()
```

- [ ] **Step 2b: Run → FAIL** (`python3 -m pytest tests/test_backfill_curator_gate_decisions.py -q` — fails: the table/script don't exist; if no DSN it skips — run via the systemd-run+.env pattern below to exercise it).

- [ ] **Step 3: Implement the backfill script** — `scripts/backfill_curator_gate_decisions.py` (APPLY-only; idempotent; prints before/after counts):
```python
#!/usr/bin/env python3
# One-time: repair the curator gate_decisions inversion (W4-3). implementable_candidate is a
# PROMOTED bucket but was recorded outcome='reject'. Flip those to 'pass' so paper_hit_rate_funnel
# curator metrics are correct. Idempotent. APPLY-only — run explicitly at the gated deploy.
import os, psycopg2
SQL = ("UPDATE paper_gate_decisions SET outcome='pass' "
       "WHERE gate_name='curator' AND outcome='reject' AND predicted_bucket='implementable_candidate'")
def main():
    dsn = os.environ["POSTGRES_URI"]
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM paper_gate_decisions WHERE gate_name='curator' AND outcome='reject' AND predicted_bucket='implementable_candidate'")
        n = cur.fetchone()[0]
        print(f"[backfill] {n} curator/implementable_candidate rows currently mislabeled 'reject'")
        cur.execute(SQL)
        print(f"[backfill] flipped {cur.rowcount} -> 'pass'")
        c.commit()
if __name__ == "__main__":
    main()
```
(Confirm `paper_gate_decisions` has a `predicted_bucket` column; if the column is named differently, adapt the test + script to the real column — Read the table's migration `src/database/migrations/033_paper_gate_decisions.sql` first.)

- [ ] **Step 4: Run → PASS.** `systemd-run --uid=0 --gid=0 -p EnvironmentFile=/root/openclaw/.env -p Environment=PYTHONPATH=/root/openclaw -p WorkingDirectory=/root/openclaw -p StandardOutput=file:/tmp/c2.out --wait --collect --quiet /usr/bin/python3 -m pytest tests/test_backfill_curator_gate_decisions.py -q` then `cat /tmp/c2.out` → 1 passed. Do NOT run the backfill script against live (deploy step).

- [ ] **Step 5: Commit (path-scoped)** — stage exactly `src/agent/curators/mastermind.js scripts/backfill_curator_gate_decisions.py tests/test_backfill_curator_gate_decisions.py` (abort guard); `fix(research): curator gate_decisions counts implementable_candidate as pass (W4-3)` + footer.

---

### Task C3: Research-queue dashboard "0 recent" (W4-4)

**Files:**
- Modify: `src/channels/api/routes_research.js:122-128` (the `/queue` `recentRuns` query)

**Interfaces — Consumes:** the `saturday_runs`/`curator_runs` shape already used by `/runs` (lines 319-360).

- [ ] **Step 1: The bug.** `/queue` (routes_research.js:109) runs `recentRuns` against `pipeline_runs WHERE run_type LIKE '%research%'…` (lines 122-128) — `pipeline_runs` only holds ticker data-collection jobs, so it ALWAYS returns 0 rows → the dashboard tile shows "0 recent". The sibling `/runs` route already sources `saturday_runs` (332) + `curator_runs` (359) correctly.

- [ ] **Step 2: Implement.** Replace the `recentRuns` query (lines 122-128) with a `saturday_runs`+`curator_runs` union projected to the `/queue` UI's expected columns (`id, run_type, status, records_written, duration_ms, created_at`):
```js
      query(
        `SELECT id, run_type, status, records_written, duration_ms, created_at FROM (
           SELECT run_id::text AS id, 'saturday-brain' AS run_type, status,
                  coded_synchronous AS records_written, NULL::int AS duration_ms, started_at AS created_at
             FROM saturday_runs ORDER BY started_at DESC LIMIT 8
         ) b
         UNION ALL
         SELECT id, run_type, status, records_written, duration_ms, created_at FROM (
           SELECT run_id::text AS id, 'corpus' AS run_type, status,
                  output_count AS records_written, NULL::int AS duration_ms, started_at AS created_at
             FROM curator_runs ORDER BY started_at DESC LIMIT 7
         ) c
         ORDER BY created_at DESC LIMIT 15`
      ),
```
(The column names `coded_synchronous`/`output_count`/`run_id`/`started_at`/`status` are copied verbatim from the `/runs` projections at routes_research.js:319-360 — confirm against that block.)

- [ ] **Step 3: Verify.** `node --check src/channels/api/routes_research.js`; `grep -n "pipeline_runs" src/channels/api/routes_research.js` → the `/queue` route no longer references it (only any unrelated routes do). Do NOT run server.js (binds :3000).

- [ ] **Step 4: Commit (path-scoped)** — stage exactly `src/channels/api/routes_research.js` (abort guard); `fix(dashboard): Research-queue recent-runs reads saturday_runs/curator_runs not pipeline_runs (W4-4)` + footer.

---

### Task C4: finisher stamps terminal research_candidates.status + backfill (W4-2)

**Files:**
- Create: `src/agent/curators/_candidate_terminal_status.js` (pure mapping helper)
- Modify: `src/agent/curators/saturday_brain_finisher.js` (wire the stamp after Tier-A code success ~247 and Tier-B stage ~296)
- Create: `scripts/backfill_research_candidate_status.py` (one-time, apply-only)
- Test: `tests/test_candidate_terminal_status.js` + `tests/test_backfill_research_candidate_status.py`

**Interfaces — Produces:** `terminalStatusFor({tier, promoted}) -> 'done'|'blocked_buildable'|null`.

- [ ] **Step 1: Pure helper test** — `tests/test_candidate_terminal_status.js`:
```js
const assert = require('assert');
const { terminalStatusFor } = require('../src/agent/curators/_candidate_terminal_status');
// Tier-A coded+promoted -> done
assert.strictEqual(terminalStatusFor({ tier: 'A', promoted: true }), 'done');
// Tier-A attempted but not promoted (coding failed) -> null (leave pending for retry, do NOT mislabel terminal)
assert.strictEqual(terminalStatusFor({ tier: 'A', promoted: false }), null);
// Tier-B staged -> blocked_buildable
assert.strictEqual(terminalStatusFor({ tier: 'B', promoted: false }), 'blocked_buildable');
// unknown tier -> null
assert.strictEqual(terminalStatusFor({ tier: 'C', promoted: false }), null);
console.log('ok test_candidate_terminal_status');
```

- [ ] **Step 2: Run → FAIL** (`node tests/test_candidate_terminal_status.js` → Cannot find module).

- [ ] **Step 3: Implement the helper** — `src/agent/curators/_candidate_terminal_status.js`:
```js
// Pure: map a finisher per-candidate outcome to the terminal research_candidates.status the
// scheduled pipeline should stamp (the pipeline historically never wrote status -> the column
// lied; W4-2). Uses ONLY the existing status state-machine values. Returns null when no
// terminal stamp applies (e.g. Tier-A coding failed -> leave 'pending' for a future retry).
function terminalStatusFor({ tier, promoted }) {
  if (tier === 'A' && promoted) return 'done';
  if (tier === 'B') return 'blocked_buildable';
  return null;
}
module.exports = { terminalStatusFor };
```

- [ ] **Step 4: Run → PASS** (`node tests/test_candidate_terminal_status.js` → ok).

- [ ] **Step 5: Wire the stamp in the finisher.** In `saturday_brain_finisher.js`: at the Tier-A success branch (after `if (outcome && outcome.promoted) { coded++; …}` ~line 247-248) and the Tier-B stage loop (after `staged++;` ~line 296), stamp the status using the helper + the existing `_query` (line 51) + `candidateId`:
```js
  // after coded++ (Tier-A promoted): stamp 'done'
  const _ts = require('./_candidate_terminal_status').terminalStatusFor({ tier: 'A', promoted: true });
  if (_ts) await _query("UPDATE research_candidates SET status=$1 WHERE candidate_id=$2", [_ts, candidateId]).catch((e)=>log(`  status-stamp[${sid}] failed: ${e.message}`));
```
and the analogous `tier:'B'` stamp (`'blocked_buildable'`) in the Tier-B loop using that loop's `candidateId`. (Require the helper once at top of the file for cleanliness; the inline require shown is for locality. Stamping is SAFE for `_hunt` — hunted rows are already excluded by `hunter_result_json IS NOT NULL`, so moving status off `'pending'` doesn't disturb re-hunt.) Verify `node --check`.

- [ ] **Step 6: Backfill test** — `tests/test_backfill_research_candidate_status.py` (rolled-back temp table; verifies the reclassify maps hunted-but-pending rows to terminal status by rejection_reason/data_tier):
```python
import os, pytest
try:
    import psycopg2
except ImportError:
    psycopg2 = None

# the reclassify the backfill applies (string-substituted table name for the temp test)
RECLASSIFY = """
UPDATE {t} SET status = CASE
    WHEN hunter_result_json->>'rejection_reason_if_any' IS NOT NULL THEN 'blocked_rejected'
    WHEN data_tier = 'A' THEN 'done'
    WHEN data_tier = 'B' THEN 'blocked_buildable'
    ELSE 'blocked_unclassified' END
  WHERE status='pending' AND hunter_result_json IS NOT NULL
    AND hunter_result_json::text NOT IN ('null','{{}}')
"""

@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_backfill_reclassifies_hunted_pending():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn: pytest.skip("POSTGRES_URI not set")
    conn = psycopg2.connect(dsn); conn.autocommit=False
    try:
        cur=conn.cursor()
        cur.execute("CREATE TEMP TABLE _rc (status text, data_tier text, hunter_result_json jsonb) ON COMMIT DROP")
        cur.execute("""INSERT INTO _rc VALUES
            ('pending','A','{\"strategy_id\":\"x\"}'),
            ('pending','B','{\"strategy_id\":\"y\"}'),
            ('pending',NULL,'{\"rejection_reason_if_any\":\"no_data\"}'),
            ('pending',NULL,NULL),
            ('done','A','{\"strategy_id\":\"z\"}')""")
        cur.execute(RECLASSIFY.format(t="_rc"))
        cur.execute("SELECT status,count(*) FROM _rc GROUP BY 1 ORDER BY 1")
        got=dict(cur.fetchall())
        assert got.get('done')==2 and got.get('blocked_buildable')==1 and got.get('blocked_rejected')==1
        assert got.get('pending')==1   # the hunter_result_json NULL row stays pending (un-hunted)
    finally:
        conn.rollback(); conn.close()
```

- [ ] **Step 7: Implement the backfill script** — `scripts/backfill_research_candidate_status.py` (APPLY-only, idempotent; the same `RECLASSIFY` against live `research_candidates`, prints before/after status histogram). Read `src/database/migrations/025_research_queues.sql` to confirm the column names (`candidate_id`, `status`, `data_tier`, `hunter_result_json`). Do NOT run against live (deploy step).

- [ ] **Step 8: Run tests → PASS.** `node tests/test_candidate_terminal_status.js`; the pytest via systemd-run+.env (as in C2 Step 4) → 1 passed. `node --check saturday_brain_finisher.js`.

- [ ] **Step 9: Commit (path-scoped)** — stage exactly `src/agent/curators/_candidate_terminal_status.js src/agent/curators/saturday_brain_finisher.js scripts/backfill_research_candidate_status.py tests/test_candidate_terminal_status.js tests/test_backfill_research_candidate_status.py` (abort guard); `feat(research): finisher stamps terminal candidate status + backfill (W4-2)` + footer.

---

## Deploy (operator-gated, after final review — NOT in these tasks)
push; the Node/Python fixes apply on the next research subprocess (fresh per run); apply the two backfills via `systemd-run … python3 scripts/backfill_*`; split the code-review into its own unit + `ExecStartPre=-pkill -f saturday_brain_finisher` + `daemon-reload`; reap the 2 zombies (`systemctl stop smoke-git-code{,2}.service`) — each explicit-approved. Verify next Sunday's code lane = `Result=success` + the code-review log updates.

## Self-Review (author)
- **Spec coverage:** W4-1→C1; W4-3→C2; W4-4→C3; W4-2→C4. systemd (W4-5/6) = deploy-gated config (noted, not a task). ✓
- **Placeholders:** pure helpers + tests have full code; the backfill scripts give full SQL; the two "confirm column names against migration NNN" notes are real verification steps, not placeholders. ✓
- **Type consistency:** `terminalStatusFor({tier,promoted})`, `HIGH_BUCKETS.has`, the backfill SQL, and the `/queue` projection columns are consistent across tasks + match the grounded source lines. ✓
