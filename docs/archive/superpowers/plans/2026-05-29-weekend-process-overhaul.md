# Weekend Process Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Couple position-recs to the backtest (auto-apply stop/TP overrides when they raise Sharpe), refresh dashboard backtest metrics weekly, reorganise the weekend into four slots, consolidate data-pipeline notifications into #data-alerts, and turn the regime-approvals page into a "Strategy Adjustments" view.

**Architecture:** Finish the orphaned per-(strategy, regime) override path (`strategy_regime_params.stop_pct/target_pct`) so both `unified_backtest` and live `engine.py` honor it; gate the read-side + the Saturday auto-apply write behind one default-OFF env gate `OPENCLAW_BACKTEST_COUPLED_RECS`. A new Saturday-morning driver sequences review → recs → backtest-coupling → backtest refresh → weights → panel rebuild. Discord routing + the dashboard page are independent.

**Tech Stack:** Python 3 (psycopg2, pandas) for backtest/execution; Node.js (express, pg) for dashboard + curators; bash + systemd timers; PostgreSQL.

**Worktree:** All work happens in `/root/.config/superpowers/worktrees/weekend-process-overhaul` (branch `feat/weekend-process-overhaul`). Run all commands from that directory: `cd /root/.config/superpowers/worktrees/weekend-process-overhaul`. Tests run with `PYTHONPATH=src python3 -m pytest <path> -v` (Python) and `node <path>` (JS smoke).

**Gate:** `OPENCLAW_BACKTEST_COUPLED_RECS` — default-OFF. OFF ⇒ byte-identical to today (overrides never read, never written). The operator flips it to `1` after completion.

---

## File Structure

**Create**
- `src/execution/regime_param_override.py` — pure helper applying a stop/target override to a signal's bracket. One responsibility: arithmetic + gate + source resolution.
- `src/execution/backtest_coupled_recs.py` — the Saturday coupling step (baseline vs candidate backtest, accept rule, apply).
- `src/database/migrations/125_param_change_backtest_cols.sql` — three nullable audit columns.
- `src/maintenance/weekend_saturday.sh` — the sequenced Saturday-morning driver.
- `docs/openclaw-weekend-saturday.{service,timer}`, `docs/openclaw-weekend-maintenance-sat.{service,timer}`, `docs/openclaw-weekend-sunday.{service,timer}`, `docs/openclaw-weekend-maintenance-sun.{service,timer}`.
- `docs/weekend-schedule-migration.md` — what was disabled.
- Tests: `tests/test_regime_param_override.py`, `tests/test_backtest_coupled_recs.py`, `tests/test_param_change_backtest_cols.py`, `tests/test_pipeline_step_routing.py`, `tests/test_sod_ingestion_summary.js`.

**Modify**
- `src/strategies/eligibility_manager.py` — `set_params` accepts + writes `bt_sharpe_before/after`, `bt_n_trades`.
- `src/backtest/unified_backtest.py` — `run_backtest(param_override=None)`; apply override in `_per_bar_simulate`.
- `src/execution/engine.py` — apply override to signals in `run_strategies` (gated).
- `src/execution/pipeline_orchestrator.py` — step boundary posts → `#data-alerts`; add cycle-start bookend to `#pipeline-feed`.
- `src/pipeline/run_collector_once.js` — shared `formatIngestionSummary`; full SOD summary.
- `src/channels/api/routes_regime_proposals.js` — `GET /applied`.
- `src/channels/api/server.js` — rename section; render "Applied this week".
- `src/agent/run_maintenance.js` — `weekend-sat` / `weekend-sun` modes.

**Task order** (cheap/independent first): T1 (migration+audit cols) → T2 (Discord step routing) → T3 (SOD summary) → T4 (Strategy Adjustments page) → T5 (override helper + backtest read-side) → T6 (live engine read-side) → T7 (coupling step) → T8 (Saturday driver + maintenance modes + panel verify) → T9 (systemd units + decommission doc).

---

## Task 1: Migration 125 + audit columns in `set_params`

**Files:**
- Create: `src/database/migrations/125_param_change_backtest_cols.sql`
- Modify: `src/strategies/eligibility_manager.py` (the `set_params` audit INSERT)
- Test: `tests/test_param_change_backtest_cols.py`

- [ ] **Step 1: Write the migration**

`src/database/migrations/125_param_change_backtest_cols.sql`:
```sql
-- 125: backtest justification columns on the regime-param audit log.
-- Carries the baseline/candidate Sharpe + trade count that justified a
-- stop/target change applied by the Saturday backtest-coupling step
-- (source='saturday_coupling'). Additive, nullable — existing rows + callers
-- are unaffected.
ALTER TABLE strategy_regime_param_changes
    ADD COLUMN IF NOT EXISTS bt_sharpe_before NUMERIC,
    ADD COLUMN IF NOT EXISTS bt_sharpe_after  NUMERIC,
    ADD COLUMN IF NOT EXISTS bt_n_trades      INT;
```

- [ ] **Step 2: Apply the migration to the local DB and verify the columns exist**

Run:
```bash
cd /root/.config/superpowers/worktrees/weekend-process-overhaul
PYTHONPATH=src python3 -c "
from database.postgres import migrate
migrate()
import psycopg2, os
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='strategy_regime_param_changes' AND column_name LIKE 'bt_%' ORDER BY 1\")
print([r[0] for r in cur.fetchall()])
"
```
Expected: `['bt_n_trades', 'bt_sharpe_after', 'bt_sharpe_before']`

- [ ] **Step 3: Write the failing test for `set_params` writing the new columns**

`tests/test_param_change_backtest_cols.py`:
```python
import os, psycopg2, pytest
from strategies import eligibility_manager as em

URI = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(not URI, reason='needs DB')
def test_set_params_writes_backtest_cols():
    sid = '__test_couple__'
    # clean any prior test rows
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("DELETE FROM strategy_regime_param_changes WHERE strategy_id=%s", (sid,))
        cur.execute("DELETE FROM strategy_regime_params WHERE strategy_id=%s", (sid,))
        c.commit()
    em.set_params(strategy_id=sid, regime_state='LOW_VOL', stop_pct=0.06,
                  actor='saturday_coupling', reason='test', source='saturday_coupling',
                  bt_sharpe_before=0.50, bt_sharpe_after=0.65, bt_n_trades=42)
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("""SELECT bt_sharpe_before, bt_sharpe_after, bt_n_trades, source
                       FROM strategy_regime_param_changes WHERE strategy_id=%s""", (sid,))
        row = cur.fetchone()
    assert row is not None
    assert float(row[0]) == 0.50 and float(row[1]) == 0.65 and row[2] == 42
    assert row[3] == 'saturday_coupling'

@pytest.mark.skipif(not URI, reason='needs DB')
def test_set_params_backtest_cols_default_null():
    """Existing callers that omit the new kwargs still work; columns are NULL."""
    sid = '__test_couple2__'
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("DELETE FROM strategy_regime_param_changes WHERE strategy_id=%s", (sid,))
        cur.execute("DELETE FROM strategy_regime_params WHERE strategy_id=%s", (sid,))
        c.commit()
    em.set_params(strategy_id=sid, regime_state='LOW_VOL', size_scalar=0.8,
                  actor='cli', reason='test')
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("""SELECT bt_sharpe_before, bt_sharpe_after, bt_n_trades
                       FROM strategy_regime_param_changes WHERE strategy_id=%s""", (sid,))
        row = cur.fetchone()
    assert row == (None, None, None)
```

- [ ] **Step 4: Run it to confirm it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_param_change_backtest_cols.py -v`
Expected: FAIL — `set_params() got an unexpected keyword argument 'bt_sharpe_before'`.

- [ ] **Step 5: Add the kwargs + audit write in `eligibility_manager.set_params`**

In `src/strategies/eligibility_manager.py`, extend the `set_params` signature (after the existing `source: str = 'cli'` line) to add:
```python
               source: str = 'cli',
               bt_sharpe_before: Optional[float] = None,
               bt_sharpe_after: Optional[float] = None,
               bt_n_trades: Optional[int] = None) -> dict:
```
Then change the audit INSERT (the `INSERT INTO strategy_regime_param_changes (actor, strategy_id, regime_state, before_row, after_row, reason, source) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)` block) to:
```python
            cur.execute("""
                INSERT INTO strategy_regime_param_changes
                    (actor, strategy_id, regime_state,
                     before_row, after_row, reason, source,
                     bt_sharpe_before, bt_sharpe_after, bt_n_trades)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            """, (actor, strategy_id, regime_state,
                  _row_to_json(before), _row_to_json(after_row),
                  reason, source,
                  bt_sharpe_before, bt_sharpe_after, bt_n_trades))
```

- [ ] **Step 6: Run the test to confirm it passes**

Run: `PYTHONPATH=src python3 -m pytest tests/test_param_change_backtest_cols.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add src/database/migrations/125_param_change_backtest_cols.sql src/strategies/eligibility_manager.py tests/test_param_change_backtest_cols.py
git commit -m "feat(regime-params): backtest-justification audit columns (mig 125)"
```

---

## Task 2: Route pipeline step-boundary posts to #data-alerts

**Files:**
- Modify: `src/execution/pipeline_orchestrator.py` (`pipeline_feed` helper + call sites L824, L868; add cycle-start bookend)
- Test: `tests/test_pipeline_step_routing.py`

**Context:** `pipeline_feed(msg)` (L376-378) posts to `#pipeline-feed`. Call sites: L824 step-START `▶️`, L868 step DONE/FAILED `✅/❌`, L907 cycle-complete `🏁`. We move the per-step posts (L824, L868) to `#data-alerts` and keep only the cycle bookend (start + complete) in `#pipeline-feed`.

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_step_routing.py`:
```python
import importlib
import execution.pipeline_orchestrator as po

def test_data_alerts_helper_targets_data_alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(po, 'post_channel', lambda ch, msg: calls.append((ch, msg)) or True)
    po.data_alerts('hello')
    assert calls == [('data-alerts', 'hello')]

def test_pipeline_feed_still_targets_pipeline_feed(monkeypatch):
    calls = []
    monkeypatch.setattr(po, 'post_channel', lambda ch, msg: calls.append((ch, msg)) or True)
    po.pipeline_feed('bookend')
    assert calls == [('pipeline-feed', 'bookend')]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_pipeline_step_routing.py -v`
Expected: FAIL — `module 'execution.pipeline_orchestrator' has no attribute 'data_alerts'`.

- [ ] **Step 3: Add the `data_alerts` helper next to `pipeline_feed`**

In `src/execution/pipeline_orchestrator.py`, immediately after the `pipeline_feed` function (ends L378), add:
```python
def data_alerts(msg):
    """Post a concise one-liner to #data-alerts (per-step boundaries + data
    notifications consolidate here; #pipeline-feed keeps only the daily cycle
    bookend). Non-blocking."""
    post_channel('data-alerts', msg)
```

- [ ] **Step 4: Repoint the two per-step boundary posts**

At L824 change `pipeline_feed(f'{reason_tag}▶️ \`{step_key}\` starting ({run_date})')` to `data_alerts(...)` (same message). At L868 change `pipeline_feed(f'{reason_tag}{icon} \`{step_key}\` {"done" if ok else "FAILED"} in {dt}s ({run_date})')` to `data_alerts(...)` (same message). Leave the cycle-complete `pipeline_feed(...)` at L907 unchanged.

- [ ] **Step 5: Add a cycle-START bookend to #pipeline-feed**

Find where the run begins emitting steps (just before the per-step loop that contains L824 — search for where `run_date`/`scope` is first known and `n_done` is initialized). Add, before the loop:
```python
        pipeline_feed(f'{reason_tag}🚀 **Cycle starting** — {run_date} ({scope})')
```
This makes `#pipeline-feed` carry exactly the start + complete bookend.

- [ ] **Step 6: Run the test + a syntax check**

Run:
```bash
PYTHONPATH=src python3 -m pytest tests/test_pipeline_step_routing.py -v
PYTHONPATH=src python3 -c "import execution.pipeline_orchestrator"
```
Expected: tests PASS; import clean (no syntax error).

- [ ] **Step 7: Commit**

```bash
git add src/execution/pipeline_orchestrator.py tests/test_pipeline_step_routing.py
git commit -m "feat(pipeline): step-boundary posts -> #data-alerts; #pipeline-feed keeps cycle bookend"
```

---

## Task 3: Full SOD ingestion summary to #data-alerts (EOD-style)

**Files:**
- Modify: `src/pipeline/run_collector_once.js` (factor `formatIngestionSummary`; enrich the SOD post)
- Test: `tests/test_sod_ingestion_summary.js` (plain-node assertion script)

**Context:** EOD posts a structured per-date breakdown via `formatEodAlert(summary, ok)` (L81-120). The SOD (full-cycle) path (L163-168) currently posts `header + last-15 phase lines`. We want SOD to post a **full per-phase ingestion summary** matching EOD's structured style, both via a shared `formatIngestionSummary`.

- [ ] **Step 1: Write the failing test (node assertion harness)**

`tests/test_sod_ingestion_summary.js`:
```javascript
'use strict';
const assert = require('assert');
// Export the formatter for testing (Step 3 adds module.exports.formatIngestionSummary).
const { formatIngestionSummary } = require('../src/pipeline/run_collector_once.js');

// SOD: full per-phase breakdown, EOD-style header.
const sodBody = formatIngestionSummary('SOD', {
  ok: true,
  elapsed_s: 412,
  date: '2026-05-29',
  phases: [
    { phase: 'prices',       rows: 426 },
    { phase: 'options',      rows: 18 },
    { phase: 'fundamentals', rows: 51 },
    { phase: 'news',         rows: 240 },
    { phase: 'sentiment',    rows: 79 },
  ],
});
assert(sodBody.includes('Start-of-day ingestion'), 'has SOD header');
assert(sodBody.includes('prices') && sodBody.includes('426'), 'lists prices rows');
assert(sodBody.includes('sentiment') && sodBody.includes('79'), 'lists every phase');
const phaseBullets = sodBody.split('\n').filter(l => l.trim().startsWith('•'));
assert.strictEqual(phaseBullets.length, 5, 'one bullet per phase (full breakdown, not last-15 truncation)');

// Failure case.
const failBody = formatIngestionSummary('SOD', { ok: false, date: '2026-05-29' });
assert(failBody.includes('FAILED'), 'failure body marked FAILED');

console.log('OK test_sod_ingestion_summary');
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `cd /root/.config/superpowers/worktrees/weekend-process-overhaul && node tests/test_sod_ingestion_summary.js`
Expected: FAIL — `formatIngestionSummary is not a function` (not yet exported).

- [ ] **Step 3: Add the shared `formatIngestionSummary` renderer**

In `src/pipeline/run_collector_once.js`, add this function near `formatEodAlert`:
```javascript
function formatIngestionSummary(kind, data) {
  // kind ∈ {'SOD','EOD'}. data.ok=false → failure line.
  const day = data && data.date ? data.date : new Date().toISOString().slice(0, 10);
  if (!data || !data.ok) {
    const label = kind === 'EOD' ? 'EOD refresh' : 'Start-of-day ingestion';
    return `❌ **${label} FAILED** — ${day}`;
  }
  const elapsed = data.elapsed_s != null ? ` · ${data.elapsed_s}s` : '';
  const lines = [];
  if (kind === 'SOD') {
    lines.push(`📦 **Start-of-day ingestion complete** — ${day}${elapsed}`);
    const phases = data.phases || [];
    if (!phases.length) {
      lines.push('(no phase output captured)');
    } else {
      for (const p of phases) {
        const rows = (p.rows != null) ? `**${p.rows}** rows` : (p.line || '');
        lines.push(`  • ${p.phase}: ${rows}`);
      }
    }
  } else {
    // EOD keeps its existing structured renderer (delegated).
    return formatEodAlert(data.summary, data.ok);
  }
  return lines.join('\n');
}
```

- [ ] **Step 4: Use it on the SOD post path + export it**

Replace the SOD branch (the `else` at L163-168 that builds `header + phaseLines`) so it calls the shared renderer. The collector's full-cycle path must surface a per-phase array; pass whatever phase data is captured (`phases` array of `{phase, rows}` if available, else map the existing `phases` `{line}` items to `{phase: line, rows: null}`):
```javascript
      } else {
        body = formatIngestionSummary('SOD', {
          ok,
          elapsed_s: total_s,
          date: new Date().toISOString().slice(0, 10),
          phases: (phases || []).map(p => (p.phase ? p : { phase: p.line, rows: null })),
        });
      }
```
At the bottom of the file add (or extend) the export so the test + any caller can import it:
```javascript
module.exports = { formatIngestionSummary, formatEodAlert };
```
(If the file is a CLI run via `require.main === module`, keep the existing run-guard so importing it for the test does NOT execute `main()`. If no guard exists, wrap the top-level `main()` invocation in `if (require.main === module) { main(); }`.)

- [ ] **Step 5: Run the test to confirm it passes**

Run: `node tests/test_sod_ingestion_summary.js`
Expected: `OK test_sod_ingestion_summary`.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/run_collector_once.js tests/test_sod_ingestion_summary.js
git commit -m "feat(collector): full per-phase SOD ingestion summary to #data-alerts (EOD-style)"
```

---

## Task 4: "Strategy Adjustments" page (rename + applied-this-week)

**Files:**
- Modify: `src/channels/api/routes_regime_proposals.js` (add `GET /applied`)
- Modify: `src/channels/api/server.js` (rename `#rp-section` header; add applied sub-table + render + fetch)
- Test: `tests/test_applied_route.js` (node assertion harness against the SQL builder)

**Context:** The pending-proposals section is `#rp-section` (server.js L4035-4050, header L4037), rendered by `_rpRender` (L8292-8338), fetched at L7918. Route file mounted at `/api/regime-proposals` (server.js L2566). DB access via `const { query } = require('../../database/postgres')`.

- [ ] **Step 1: Add the `GET /applied` handler (route)**

In `src/channels/api/routes_regime_proposals.js`, before `module.exports = router;`, add:
```javascript
// Applied-this-week: stop/target overrides auto-applied by the Saturday
// backtest-coupling step (source='saturday_coupling'), with the ΔSharpe that
// justified each. Read-only; powers the "Strategy Adjustments → Applied this
// week" table.
router.get('/applied', async (req, res) => {
  const days = Math.min(parseInt(req.query.days, 10) || 7, 90);
  try {
    const result = await query(`
      SELECT id, changed_at, strategy_id, regime_state,
             (before_row->>'stop_pct')::float   AS stop_before,
             (after_row ->>'stop_pct')::float    AS stop_after,
             (before_row->>'target_pct')::float  AS target_before,
             (after_row ->>'target_pct')::float  AS target_after,
             bt_sharpe_before::float             AS bt_sharpe_before,
             bt_sharpe_after::float              AS bt_sharpe_after,
             bt_n_trades, reason
        FROM strategy_regime_param_changes
       WHERE source = 'saturday_coupling'
         AND changed_at >= NOW() - ($1 || ' days')::interval
       ORDER BY changed_at DESC, strategy_id, regime_state
    `, [String(days)]);
    res.json({ applied: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
```

- [ ] **Step 2: Smoke-test the route handler wiring**

`tests/test_applied_route.js`:
```javascript
'use strict';
const assert = require('assert');
const router = require('../src/channels/api/routes_regime_proposals.js');
// The router is an express.Router; assert the /applied GET layer is registered.
const layers = (router.stack || []).filter(l => l.route && l.route.path === '/applied');
assert.strictEqual(layers.length, 1, 'GET /applied route registered');
assert(layers[0].route.methods.get, '/applied is a GET');
console.log('OK test_applied_route');
```
Run: `node tests/test_applied_route.js`
Expected: first run before Step 1 would FAIL (`0` layers); after Step 1, `OK test_applied_route`. Run it now to confirm PASS.

- [ ] **Step 3: Rename the section header + add the Applied sub-table (server.js HTML)**

In `src/channels/api/server.js`, replace the `#rp-section` header block (L4036-4039) and insert the applied table above the pending table. Change:
```html
      <div class="pf-section-header">
        <span>📋 Pending Regime Proposals <span class="st-sub-label" id="rp-count">—</span></span>
        <span class="st-sub-label">from MastermindJohn Saturday review</span>
      </div>
```
to:
```html
      <div class="pf-section-header">
        <span>⚙️ Strategy Adjustments</span>
        <span class="st-sub-label">applied stop/TP changes + pending weight/eligibility proposals</span>
      </div>
      <div class="st-sub-label" style="margin:4px 0">Applied this week <span id="sa-applied-count">—</span></div>
      <table id="sa-applied-table" style="border-collapse:collapse;width:100%;font-size:12px;margin-bottom:14px">
        <thead><tr style="text-align:left;color:var(--muted);font-size:11px">
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Strategy</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Regime</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Stop →</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Target →</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">ΔSharpe</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">Trades</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="st-sub-label" style="margin:4px 0">Pending proposals <span class="st-sub-label" id="rp-count">—</span></div>
```
Leave the existing `<table id="rp-table" ...>` (L4040+) exactly as-is below this.

- [ ] **Step 4: Add the applied-table render function + fetch**

In `src/channels/api/server.js`, add a render function near `_rpRender` (after L8338):
```javascript
function _saRenderApplied(applied) {
  const tbody = document.querySelector('#sa-applied-table tbody');
  const countEl = document.getElementById('sa-applied-count');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (countEl) countEl.textContent = '(' + applied.length + ')';
  if (!applied.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="padding:6px 8px;color:var(--muted)">No stop/TP changes applied in the last 7 days.</td></tr>';
    return;
  }
  const fmt = (v) => (v == null ? '—' : Number(v).toFixed(3));
  for (const a of applied) {
    const d = (a.bt_sharpe_after != null && a.bt_sharpe_before != null)
      ? (a.bt_sharpe_after - a.bt_sharpe_before) : null;
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + a.strategy_id + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + a.regime_state + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + fmt(a.stop_before) + ' → ' + fmt(a.stop_after) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + fmt(a.target_before) + ' → ' + fmt(a.target_after) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;color:' + (d != null && d >= 0 ? '#2ea043' : 'var(--muted)') + '">' + (d != null ? (d >= 0 ? '+' : '') + d.toFixed(2) : '—') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right">' + (a.bt_n_trades != null ? a.bt_n_trades : '—') + '</td>';
    tbody.appendChild(tr);
  }
}
```
Then find the proposals fetch (L7918, `_safeFetch('/api/regime-proposals?status=pending', ...)`) and add a sibling fetch in the same batch:
```javascript
      _safeFetch('/api/regime-proposals/applied?days=7', { applied: [] }, { label: 'applied adjustments' }),
```
Wherever the resolved proposals are passed to `_rpRender(...)`, call `_saRenderApplied(<appliedResult>.applied)` alongside it (the section is shown whenever either applied rows OR pending proposals exist — adjust the `section.style.display` guard in `_rpRender` so an empty pending list no longer hides the whole section when applied rows exist; simplest: set `section.style.display = ''` from the applied render and remove the early `return` hide in `_rpRender` when proposals is empty, leaving the pending table simply empty).

- [ ] **Step 5: Verify server.js parses + smoke the route test**

Run:
```bash
cd /root/.config/superpowers/worktrees/weekend-process-overhaul
node --check src/channels/api/server.js
node tests/test_applied_route.js
```
Expected: `node --check` prints nothing (valid); `OK test_applied_route`.

- [ ] **Step 6: Commit**

```bash
git add src/channels/api/routes_regime_proposals.js src/channels/api/server.js tests/test_applied_route.js
git commit -m "feat(dashboard): Strategy Adjustments page — applied stop/TP changes + ΔSharpe"
```

---

## Task 5: Override helper + backtest read-side (`param_override`)

**Files:**
- Create: `src/execution/regime_param_override.py`
- Modify: `src/backtest/unified_backtest.py` (`run_backtest(param_override=None)`; thread to `_per_bar_simulate`; apply before L553-556)
- Test: `tests/test_regime_param_override.py`

**Context:** `run_backtest` already threads `resolver=None` (L644 sig → L683-688 call → used inside `_per_bar_simulate`). We add `param_override=None` identically. In `_per_bar_simulate`, `regime_state` is known per bar (L491) and the signal's `stop_loss`/`target_1` are computed at L553-556 inside the `for sig in signals` loop (L540+). Override semantics: absolute-replace the bracket as a flat distance from entry. Gate: `OPENCLAW_BACKTEST_COUPLED_RECS`.

- [ ] **Step 1: Write the failing test for the pure helper**

`tests/test_regime_param_override.py`:
```python
import os
import pytest
from execution import regime_param_override as rpo

def test_apply_override_long_replaces_bracket():
    # long: stop below entry by stop_pct, target above by target_pct
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0,
        override={'stop_pct': 0.05, 'target_pct': 0.10})
    assert stop == pytest.approx(95.0)    # 100 * (1 - 0.05)
    assert target == pytest.approx(110.0) # 100 * (1 + 0.10)

def test_apply_override_short_replaces_bracket():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=-1, stop_loss=102.0, target_1=96.0,
        override={'stop_pct': 0.05, 'target_pct': 0.10})
    assert stop == pytest.approx(105.0)   # short stop above entry
    assert target == pytest.approx(90.0)  # short target below entry

def test_apply_override_none_passthrough():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0,
        override=None)
    assert stop == 98.0 and target == 104.0

def test_apply_override_partial_keeps_other_leg():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0,
        override={'stop_pct': 0.05})  # only stop overridden
    assert stop == pytest.approx(95.0)
    assert target == 104.0

def test_resolve_override_gate_off_returns_none(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BACKTEST_COUPLED_RECS', raising=False)
    assert rpo.resolve_override('S_x', 'LOW_VOL', injected=None) is None

def test_resolve_override_injected_map_used(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    inj = {'LOW_VOL': {'stop_pct': 0.06, 'target_pct': 0.09}}
    assert rpo.resolve_override('S_x', 'LOW_VOL', injected=inj) == {'stop_pct': 0.06, 'target_pct': 0.09}
    # regime not in map → None
    assert rpo.resolve_override('S_x', 'HIGH_VOL', injected=inj) is None
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_regime_param_override.py -v`
Expected: FAIL — `No module named 'execution.regime_param_override'`.

- [ ] **Step 3: Implement the pure helper**

`src/execution/regime_param_override.py`:
```python
"""Apply a per-(strategy, regime) stop/target override to a signal bracket.

Single shared implementation imported by both the backtest (unified_backtest)
and live execution (engine). Gated by OPENCLAW_BACKTEST_COUPLED_RECS: when the
gate is OFF, resolve_override() always returns None and brackets are untouched
(byte-identical to pre-coupling behaviour).

Override semantics: ABSOLUTE-REPLACE. The override's stop_pct / target_pct are
flat distances from entry (fractions). For a long, stop = entry*(1-stop_pct),
target = entry*(1+target_pct); mirrored for a short. Matches size_scalar's
established precedent (regime_param_resolver) and the resolver getters.
"""
from __future__ import annotations
import os
from typing import Optional

GATE_ENV = 'OPENCLAW_BACKTEST_COUPLED_RECS'


def gate_on() -> bool:
    return os.environ.get(GATE_ENV) == '1'


def resolve_override(strategy_id: str, regime_state: str, *,
                     injected: Optional[dict] = None) -> Optional[dict]:
    """Return {'stop_pct': x?, 'target_pct': y?} or None.

    - gate OFF → None (no override anywhere).
    - injected is a {regime: {stop_pct, target_pct}} map → use it, ignore DB
      (the coupling step's candidate path; no persisted read).
    - injected is None → read persisted strategy_regime_params via
      regime_param_resolver (live + baseline path).
    """
    if not gate_on():
        return None
    if injected is not None:
        row = injected.get(regime_state)
        return dict(row) if row else None
    from execution import regime_param_resolver as rpr
    stop = rpr.stop_pct_override(strategy_id, regime_state)
    target = rpr.target_pct_override(strategy_id, regime_state)
    if stop is None and target is None:
        return None
    out = {}
    if stop is not None:
        out['stop_pct'] = stop
    if target is not None:
        out['target_pct'] = target
    return out


def apply_override(*, entry_price: float, direction: int,
                   stop_loss: float, target_1: float,
                   override: Optional[dict]) -> tuple[float, float]:
    """Return (stop_loss, target_1) after applying the override (or unchanged
    when override is None / a leg is absent). direction: +1 long, -1 short."""
    if not override:
        return stop_loss, target_1
    s, t = stop_loss, target_1
    sp = override.get('stop_pct')
    tp = override.get('target_pct')
    if sp is not None:
        s = entry_price * (1 - sp) if direction > 0 else entry_price * (1 + sp)
    if tp is not None:
        t = entry_price * (1 + tp) if direction > 0 else entry_price * (1 - tp)
    return s, t
```

- [ ] **Step 4: Run the helper test to confirm it passes**

Run: `PYTHONPATH=src python3 -m pytest tests/test_regime_param_override.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Thread `param_override` + `return_metrics` through `run_backtest` → `_per_bar_simulate`**

In `src/backtest/unified_backtest.py`:
- Add to the `run_backtest` signature (after `resolver=None,` at L644): `param_override=None,` and `return_metrics: bool = False,`
- In the `_simulate_for(...)` call (L683-688), add the kwarg: pass `param_override=param_override,` alongside `resolver=resolver,`.
- Add `param_override=None,` to the `_per_bar_simulate` signature (after `resolver=None,` at L439).
- **Ephemeral-run contract (load-bearing, VERIFIED against the code):** the persist INSERTs at L719-805 run unconditionally inside the `try`, but `conn.commit()` is gated by `if commit:` (L807) and the own-conn `conn.close()` (L814) rolls back when not committed — so `commit=False` (with NO `conn` passed, i.e. own-conn) leaves nothing persisted. **However the panel rebuild at L821-825 runs unconditionally** — wrap it in `if commit:` so ephemeral coupling runs never rebuild the dashboard panel:
  ```python
      if commit:
          try:
              from backtest.backtest_panel import rebuild as _rebuild_panel
              _rebuild_panel(strategy_id)
          except Exception as _e:
              print(f'[unified_backtest] panel rebuild skipped: {_e}')
      return run_id
  ```
- When `return_metrics=True`, return `(run_id, total_metrics)` where `total_metrics` is `aggregate_metrics(trades)` (L705) **augmented with median bracket distances** computed from the run's trades (each trade dict has `signal_stop`, `signal_target`, `entry_price` — L785-786, L581):
  ```python
      if return_metrics:
          import statistics
          sd = [abs(t['entry_price'] - t['signal_stop']) / t['entry_price']
                for t in trades if t.get('signal_stop') and t.get('entry_price')]
          td = [abs(t['signal_target'] - t['entry_price']) / t['entry_price']
                for t in trades if t.get('signal_target') and t.get('entry_price')]
          total_metrics = {**total_metrics,
                           'median_stop_pct':   (statistics.median(sd) if sd else None),
                           'median_target_pct': (statistics.median(td) if td else None)}
          return run_id, total_metrics
  ```
  Place this `return` immediately before the existing `return run_id` (after the `if commit:` rebuild block). When `return_metrics=False` (default, all existing callers), return `run_id` exactly as today (signature-compatible).

- [ ] **Step 6: Apply the override inside the simulate loop**

In `_per_bar_simulate`, inside `for sig in signals[...]:`, immediately after `target_1 = ...` (L555-556) and before the defensive direction check (L560), insert:
```python
            _ov = regime_param_override.resolve_override(
                strategy_id, str(regime_state), injected=param_override)
            if _ov:
                stop_loss, target_1 = regime_param_override.apply_override(
                    entry_price=entry_price, direction=direction,
                    stop_loss=stop_loss, target_1=target_1, override=_ov)
```
Add the import at the top of `unified_backtest.py` (with the other `from execution import ...` / local imports): `from execution import regime_param_override`.

- [ ] **Step 7: Write a backtest-level test that the injected override changes the exit**

Append to `tests/test_regime_param_override.py`:
```python
@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='needs DB + prices')
def test_injected_override_changes_backtest(monkeypatch):
    """A much tighter injected stop must change sharpe/trades vs baseline for a
    real live strategy, using ephemeral (commit=False) metric-returning runs."""
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    from backtest import unified_backtest as ub
    import json, pathlib
    man = json.loads(pathlib.Path('src/strategies/manifest.json').read_text())
    live = [s['id'] for s in man.get('strategies', []) if s.get('state') == 'live']
    if not live:
        pytest.skip('no live strategies')
    sid = live[0]
    _b, base_m = ub.run_backtest(sid, commit=False, return_metrics=True)
    _t, tight_m = ub.run_backtest(sid, commit=False, return_metrics=True, param_override={
        r: {'stop_pct': 0.01, 'target_pct': 0.50} for r in
        ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')})
    # Ephemeral runs must not persist (no panel pollution); both return metrics.
    assert 'sharpe' in base_m and 'sharpe' in tight_m
    # A 1% stop on every signal should change behaviour vs the native bracket.
    assert (base_m['sharpe'] != tight_m['sharpe']) or (base_m['total_trades'] != tight_m['total_trades'])
```
*(Implementer note: the load-bearing assertion is that the injected-override run executes the new code path and is distinguishable from baseline. If a chosen strategy yields identical metrics by coincidence, pick another live id.)*

- [ ] **Step 8: Run tests + import check**

Run:
```bash
PYTHONPATH=src python3 -m pytest tests/test_regime_param_override.py -v
PYTHONPATH=src python3 -c "import backtest.unified_backtest"
```
Expected: helper tests PASS; the DB-gated test passes or skips; import clean.

- [ ] **Step 9: Verify gate-OFF byte-identity**

Run a quick equality check that with the gate OFF, `resolve_override` is None so the simulate path is unchanged:
```bash
PYTHONPATH=src python3 -c "
import os; os.environ.pop('OPENCLAW_BACKTEST_COUPLED_RECS', None)
from execution import regime_param_override as r
assert r.resolve_override('S_x','LOW_VOL', injected={'LOW_VOL':{'stop_pct':0.01}}) is None
print('gate-off OK')
"
```
Expected: `gate-off OK`.

- [ ] **Step 10: Commit**

```bash
git add src/execution/regime_param_override.py src/backtest/unified_backtest.py tests/test_regime_param_override.py
git commit -m "feat(backtest): honor per-regime stop/target override via param_override (gated)"
```

---

## Task 6: Live engine honors the override (gated)

**Files:**
- Modify: `src/execution/engine.py` (`run_strategies`: apply override to each signal post-generation)
- Test: `tests/test_engine_override.py`

**Context:** `run_strategies` (L630-662) generates signals per strategy with the correct per-strategy regime string (`strat_regime_str`, equity at L652). Mutating each `Signal`'s `stop_loss`/`target_1` here is the single live application point — the downstream `execution_signals` INSERT (L789-803) reads `sig.stop_loss`/`sig.target_1` and picks up the mutation. Gated by the same env var (via `regime_param_override.resolve_override`, which returns None when the gate is OFF).

- [ ] **Step 1: Write the failing test**

`tests/test_engine_override.py`:
```python
import os, types
import pytest
from dataclasses import dataclass

@dataclass
class _Sig:
    ticker: str = 'AAA'
    direction: str = 'LONG'
    entry_price: float = 100.0
    stop_loss: float = 98.0
    target_1: float = 104.0
    target_2: float = 0.0
    target_3: float = 0.0
    position_size_pct: float = 1.0

def test_apply_overrides_to_signals_gate_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    import importlib
    import execution.engine as eng
    importlib.reload(eng)
    # Stub the resolver to return a tight override for LOW_VOL.
    from execution import regime_param_override as rpo
    monkeypatch.setattr(rpo, 'resolve_override',
                        lambda sid, regime, injected=None: {'stop_pct': 0.02, 'target_pct': 0.20})
    sig = _Sig()
    eng._apply_regime_overrides_to_signals('S_demo', [sig], 'LOW_VOL')
    assert sig.stop_loss == pytest.approx(98.0)   # 100*(1-0.02)
    assert sig.target_1 == pytest.approx(120.0)   # 100*(1+0.20)

def test_apply_overrides_noop_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BACKTEST_COUPLED_RECS', raising=False)
    import importlib, execution.engine as eng
    importlib.reload(eng)
    sig = _Sig()
    eng._apply_regime_overrides_to_signals('S_demo', [sig], 'LOW_VOL')
    assert sig.stop_loss == 98.0 and sig.target_1 == 104.0
```
*(Note: 100*(1-0.02)=98.0 coincides with the original 98.0; the target assertion is the load-bearing one. Keep both.)*

- [ ] **Step 2: Run it to confirm it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_override.py -v`
Expected: FAIL — `module 'execution.engine' has no attribute '_apply_regime_overrides_to_signals'`.

- [ ] **Step 3: Add the helper + call it in `run_strategies`**

In `src/execution/engine.py`, add near the top imports: `from execution import regime_param_override`. Add a module-level helper:
```python
def _apply_regime_overrides_to_signals(strategy_id, signals, regime_state):
    """Mutate each Signal's stop_loss/target_1 with the per-(strategy, regime)
    override (gated; no-op when OPENCLAW_BACKTEST_COUPLED_RECS is unset). Mirrors
    the backtest's simulate-time application so live and backtest agree."""
    ov = regime_param_override.resolve_override(strategy_id, str(regime_state))
    if not ov:
        return
    for sig in signals or []:
        d = 1 if str(sig.direction).upper() == 'LONG' else -1
        ep = float(sig.entry_price) if getattr(sig, 'entry_price', 0) else 0.0
        if ep <= 0:
            continue
        sig.stop_loss, sig.target_1 = regime_param_override.apply_override(
            entry_price=ep, direction=d,
            stop_loss=float(sig.stop_loss or 0), target_1=float(sig.target_1 or 0),
            override=ov)
```
Then in `run_strategies`, right after `signals = strat.generate_signals(prices, strat_regime, universe, aux_data)` (L656), insert:
```python
            _apply_regime_overrides_to_signals(strat.id, signals, strat_regime_str)
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_override.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Import check**

Run: `PYTHONPATH=src python3 -c "import execution.engine"`
Expected: clean import.

- [ ] **Step 6: Commit**

```bash
git add src/execution/engine.py tests/test_engine_override.py
git commit -m "feat(engine): live signals honor per-regime stop/target override (gated)"
```

---

## Task 7: Backtest-coupling step

**Files:**
- Create: `src/execution/backtest_coupled_recs.py`
- Test: `tests/test_backtest_coupled_recs.py`

**Context:** Reads fresh `strategy_sizing_recommendations` (today's `rec_date`), for each strategy with a non-noise stop/TP delta runs baseline vs candidate backtests, applies winners via `eligibility_manager.set_params(... source='saturday_coupling', bt_*=...)` to every eligible regime, marks the rec `action_taken`. Accept rule: `ΔSharpe ≥ 0.10` AND `candidate_n_trades ≥ 30`. Gated by `OPENCLAW_BACKTEST_COUPLED_RECS` (the module refuses to apply when OFF). Derivation uses base override-or-default × (1+delta), clamped [0.01, 0.30].

- [ ] **Step 1: Write failing tests for the pure decision logic**

`tests/test_backtest_coupled_recs.py`:
```python
import pytest
from execution import backtest_coupled_recs as bc

def test_candidate_pct_uses_default_when_no_base():
    # base_stop default 0.07; delta +0.10 → 0.077
    assert bc.candidate_pct(base=None, delta=0.10, default=0.07) == pytest.approx(0.077)

def test_candidate_pct_uses_existing_base():
    assert bc.candidate_pct(base=0.05, delta=0.20, default=0.07) == pytest.approx(0.06)

def test_candidate_pct_clamped():
    assert bc.candidate_pct(base=0.07, delta=5.0, default=0.07) == 0.30      # upper clamp
    assert bc.candidate_pct(base=0.07, delta=-0.99, default=0.07) == 0.01    # lower clamp

def test_candidate_pct_none_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=None, default=0.07) is None

def test_candidate_pct_noise_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=0.004, default=0.07) is None    # |delta|<0.005

def test_accept_rule():
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=30) is True
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.59, candidate_n_trades=30) is False  # Δ<0.10
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=29) is False  # trades<30
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.65, candidate_n_trades=100) is True

def test_skip_when_no_stop_or_target_delta():
    rec = {'stop_delta_pct': None, 'target_delta_pct': None}
    assert bc.has_actionable_delta(rec) is False
    assert bc.has_actionable_delta({'stop_delta_pct': 0.05, 'target_delta_pct': None}) is True
```

- [ ] **Step 2: Run to confirm failure**

Run: `PYTHONPATH=src python3 -m pytest tests/test_backtest_coupled_recs.py -v`
Expected: FAIL — `No module named 'execution.backtest_coupled_recs'`.

- [ ] **Step 3: Implement the module**

`src/execution/backtest_coupled_recs.py`:
```python
"""Saturday backtest-coupling step.

For each fresh stop/TP recommendation, backtest the change and apply it to
strategy_regime_params (all eligible regimes) IFF it raises Sharpe by >= MIN_DELTA
on >= MIN_TRADES trades. Gated by OPENCLAW_BACKTEST_COUPLED_RECS — refuses to
apply when the gate is OFF.

Accept rule: candidate_sharpe - baseline_sharpe >= 0.10 AND candidate_n_trades >= 30.
"""
from __future__ import annotations
import os
from typing import Optional

MIN_DELTA = 0.10
MIN_TRADES = 30
NOISE = 0.005
DEFAULT_STOP_PCT = 0.07     # unified_backtest long-stop default (entry*0.93)
DEFAULT_TARGET_PCT = 0.08   # unified_backtest target default (entry*1.08)
CLAMP_LO, CLAMP_HI = 0.01, 0.30
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def gate_on() -> bool:
    return os.environ.get('OPENCLAW_BACKTEST_COUPLED_RECS') == '1'


def has_actionable_delta(rec: dict) -> bool:
    for k in ('stop_delta_pct', 'target_delta_pct'):
        v = rec.get(k)
        if v is not None and abs(float(v)) >= NOISE:
            return True
    return False


def candidate_pct(base: Optional[float], delta: Optional[float],
                  default: float) -> Optional[float]:
    if delta is None or abs(float(delta)) < NOISE:
        return None
    b = float(base) if base is not None else default
    return max(CLAMP_LO, min(CLAMP_HI, b * (1 + float(delta))))


def qualifies(*, baseline_sharpe: float, candidate_sharpe: float,
              candidate_n_trades: int) -> bool:
    return (candidate_sharpe - baseline_sharpe) >= MIN_DELTA and candidate_n_trades >= MIN_TRADES


# ── DB-touching orchestration (integration; covered by the live dry-run, not unit) ──

def _eligible_regimes(strategy_id) -> list:
    """Regimes the strategy is eligible in; all four if none seeded."""
    from execution import regime_param_resolver as rpr
    elig = [r for r in CANONICAL_REGIMES if rpr.is_eligible(strategy_id, r)]
    return elig or list(CANONICAL_REGIMES)


def _run_metrics(strategy_id, param_override) -> dict:
    """Ephemeral backtest → metrics dict (sharpe, total_trades, median_stop_pct,
    median_target_pct). commit=False (own-conn) so probe runs roll back — they do
    NOT persist to strategy_backtest_runs nor rebuild the dashboard panel. The
    authoritative panel-populating run is the post-apply --all-live refresh."""
    from backtest import unified_backtest as ub
    _run_id, metrics = ub.run_backtest(strategy_id, commit=False,
                                       param_override=param_override,
                                       return_metrics=True)
    return metrics


def _load_recs(rec_date=None) -> list:
    import psycopg2
    sql = """SELECT id, strategy_id, stop_delta_pct, target_delta_pct, reasoning
               FROM strategy_sizing_recommendations
              WHERE action_taken = 'pending'"""
    params = []
    if rec_date:
        sql += " AND rec_date = %s"; params.append(rec_date)
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mark(rec_id, status, note):
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("""UPDATE strategy_sizing_recommendations
                          SET action_taken=%s, reasoning=COALESCE(reasoning,'')|| %s
                        WHERE id=%s""", (status, ' | ' + note, rec_id))
        c.commit()


def run(rec_date=None, dry_run: bool = False, log=print) -> dict:
    if not gate_on():
        log('[coupling] OPENCLAW_BACKTEST_COUPLED_RECS off — skipping (no-op).')
        return {'skipped': True}
    from strategies import eligibility_manager as em
    recs = _load_recs(rec_date)
    applied, rejected = 0, 0
    for rec in recs:
        sid = rec['strategy_id']
        if not has_actionable_delta(rec):
            continue
        # Baseline first: its median bracket distances ARE the strategy's current
        # effective stop/target (already reflect any prior override, gate is ON).
        base = _run_metrics(sid, None)
        base_sharpe = float(base.get('sharpe') or 0.0)
        cand_stop = candidate_pct(base.get('median_stop_pct'), rec.get('stop_delta_pct'), DEFAULT_STOP_PCT)
        cand_tgt = candidate_pct(base.get('median_target_pct'), rec.get('target_delta_pct'), DEFAULT_TARGET_PCT)
        if cand_stop is None and cand_tgt is None:
            continue
        regimes = _eligible_regimes(sid)
        cand_map = {r: {k: v for k, v in (('stop_pct', cand_stop), ('target_pct', cand_tgt)) if v is not None}
                    for r in regimes}
        cand = _run_metrics(sid, cand_map)
        cand_sharpe = float(cand.get('sharpe') or 0.0)
        cand_n = int(cand.get('total_trades') or 0)
        ok = qualifies(baseline_sharpe=base_sharpe, candidate_sharpe=cand_sharpe, candidate_n_trades=cand_n)
        note = f'ΔSharpe {cand_sharpe - base_sharpe:+.3f} ({base_sharpe:.2f}->{cand_sharpe:.2f}), n={cand_n}'
        log(f'[coupling] {sid}: stop={cand_stop} target={cand_tgt} {note} -> {"APPLY" if ok else "reject"}')
        if not ok:
            if not dry_run:
                _mark(rec['id'], 'ignored', 'coupling reject ' + note)
            rejected += 1
            continue
        if dry_run:
            applied += 1
            continue
        for r in regimes:
            em.set_params(strategy_id=sid, regime_state=r,
                          stop_pct=cand_stop, target_pct=cand_tgt,
                          actor='saturday_coupling',
                          reason='backtest-coupled stop/TP: ' + note,
                          source='saturday_coupling',
                          bt_sharpe_before=base_sharpe, bt_sharpe_after=cand_sharpe,
                          bt_n_trades=cand_n)
        _mark(rec['id'], 'applied', 'coupling apply ' + note)
        applied += 1
    log(f'[coupling] done: {applied} applied, {rejected} rejected, {len(recs)} recs scanned.')
    return {'applied': applied, 'rejected': rejected, 'scanned': len(recs)}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    run(rec_date=a.date, dry_run=a.dry_run)
```

- [ ] **Step 4: Run the unit tests to confirm pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_backtest_coupled_recs.py -v`
Expected: PASS (all decision-logic tests). The DB orchestration (`run`) is validated by the Task 8 dry-run, not unit-mocked.

- [ ] **Step 5: Import + gate-off no-op check**

Run:
```bash
PYTHONPATH=src python3 -c "import execution.backtest_coupled_recs as b; print(b.run())"
```
Expected: prints `[coupling] ... off — skipping` and `{'skipped': True}` (gate OFF in shell).

- [ ] **Step 6: Commit**

```bash
git add src/execution/backtest_coupled_recs.py tests/test_backtest_coupled_recs.py
git commit -m "feat(coupling): backtest-gated auto-apply of stop/TP recommendations"
```

---

## Task 8: Saturday driver + maintenance modes + weekly panel verify

**Files:**
- Create: `src/maintenance/weekend_saturday.sh`
- Modify: `src/agent/run_maintenance.js` (add `weekend-sat`, `weekend-sun` modes)
- Test: `tests/test_weekend_saturday_dryrun.sh` (bash dry-run smoke)

**Context:** `refresh_backtests.sh` already does `unified_backtest --all-live` (rebuilds panels inline) + `eligibility_assigner --all`. The new driver sequences the analytical cluster around it. `run_maintenance.js` dispatches modes via `PROMPT_BY_MODE` (L455-459), `COST_CAP_BY_MODE` (L67-71), `TIMEOUT_MS_BY_MODE` (L72-76).

- [ ] **Step 1: Write the Saturday driver**

`src/maintenance/weekend_saturday.sh`:
```bash
#!/usr/bin/env bash
#
# Saturday 08:00 ET — Strategy Adjustment + Backtest Refresh pipeline.
# Sequenced: review -> critique -> position-recs -> backtest-coupling ->
# full backtest refresh -> weekly weights -> panel rebuild/verify -> universe-recs.
# Steps 1-4 WARN-and-continue; step 5 (backtest refresh) failing aborts 6-7.
set -uo pipefail
cd /root/openclaw
export PYTHONPATH=src
LOG="/var/log/openclaw/weekend_saturday_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
DRY="${1:-}"   # pass --dry-run to skip live writes where supported

step() { echo "[weekend_saturday] $(date -u +%FT%TZ) >>> $*" | tee -a "$LOG"; }

step "1/8 comprehensive-review"
node src/agent/curators/run_mastermind.js --mode comprehensive-review 2>&1 | tee -a "$LOG" || step "WARN review rc=$?"

step "2/8 critique"
node src/agent/curators/run_mastermind.js --mode critique 2>&1 | tee -a "$LOG" || step "WARN critique rc=$?"

step "3/8 position-recs"
node src/agent/curators/run_mastermind.js --mode position-recs 2>&1 | tee -a "$LOG" || step "WARN position-recs rc=$?"

step "4/8 backtest-coupling"
python3 -m execution.backtest_coupled_recs $DRY 2>&1 | tee -a "$LOG" || step "WARN coupling rc=$?"

step "5/8 full backtest refresh"
bash src/maintenance/refresh_backtests.sh 2>&1 | tee -a "$LOG"
BT_RC=${PIPESTATUS[0]}
if [ "$BT_RC" -ne 0 ]; then
  step "ABORT backtest refresh rc=$BT_RC — skipping weights/panels"
  exit "$BT_RC"
fi

step "6/8 weekly strategy weights"
node src/agent/curators/weekly_live_sharpe.js 2>&1 | tee -a "$LOG" || step "WARN weights rc=$?"

step "7/8 panel rebuild + verify"
python3 -m backtest.backtest_panel --rebuild 2>&1 | tee -a "$LOG" || step "WARN panel rebuild rc=$?"
python3 - <<'PY' 2>&1 | tee -a "$LOG"
import os, psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute("""SELECT COUNT(*) FROM strategy_backtest_panel p
               JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_at FROM strategy_backtest_runs
                     WHERE primary_window=TRUE ORDER BY strategy_id, run_at DESC) r
                 ON r.strategy_id=p.strategy_id
              WHERE p.computed_at < r.run_at""")
stale=cur.fetchone()[0]
print(f'[weekend_saturday] panel staleness check: {stale} panels older than their run')
PY

step "8/8 universe-recs (gated)"
node src/agent/curators/run_mastermind.js --mode universe-recs 2>&1 | tee -a "$LOG" || step "WARN universe-recs rc=$?"

step "DONE log=$LOG"
```
Make it executable: `chmod +x src/maintenance/weekend_saturday.sh`.

- [ ] **Step 2: Write the dry-run smoke test**

`tests/test_weekend_saturday_dryrun.sh`:
```bash
#!/usr/bin/env bash
# Smoke: the driver script parses and its step list is in the right order.
set -e
SCRIPT="$(dirname "$0")/../src/maintenance/weekend_saturday.sh"
bash -n "$SCRIPT"   # syntax check
grep -q 'mode comprehensive-review' "$SCRIPT"
grep -q 'mode critique' "$SCRIPT"
grep -q 'mode position-recs' "$SCRIPT"
grep -q 'execution.backtest_coupled_recs' "$SCRIPT"
grep -q 'refresh_backtests.sh' "$SCRIPT"
grep -q 'weekly_live_sharpe.js' "$SCRIPT"
grep -q 'backtest_panel --rebuild' "$SCRIPT"
grep -q 'mode universe-recs' "$SCRIPT"
# Order: coupling must come BEFORE refresh (so refresh reflects applied overrides)
awk '/backtest_coupled_recs/{c=NR} /refresh_backtests.sh/{r=NR} END{exit !(c<r)}' "$SCRIPT"
echo "OK test_weekend_saturday_dryrun"
```

- [ ] **Step 3: Run the smoke test**

Run: `bash tests/test_weekend_saturday_dryrun.sh`
Expected: `OK test_weekend_saturday_dryrun`.

- [ ] **Step 4: Add `weekend-sat` / `weekend-sun` maintenance modes**

In `src/agent/run_maintenance.js`:
- Add to `COST_CAP_BY_MODE` (after the `saturday-verify` entry): `'weekend-sat': parseFloat(process.env.MAINT_COST_CAP_USD_WEEKEND_SAT || '8.00'), 'weekend-sun': parseFloat(process.env.MAINT_COST_CAP_USD_WEEKEND_SUN || '15.00'),`
- Add to `TIMEOUT_MS_BY_MODE`: `'weekend-sat': parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_WEEKEND_SAT || '1800000', 10), 'weekend-sun': parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_WEEKEND_SUN || '3000000', 10),`
- Add two prompt constants modeled on `SATURDAY_PROMPT`:
  - `WEEKEND_SAT_PROMPT` — audits the Saturday 08:00 strategy-adjustment + backtest pipeline: confirm `strategy_memos` written today, `strategy_sizing_recommendations` present, coupling ran (check `strategy_regime_param_changes` for `source='saturday_coupling'` rows today), backtests refreshed (latest `strategy_backtest_runs.run_at` is today), `strategy_weights_by_regime` refreshed, every live strategy has a fresh `strategy_backtest_panel`. Surface gaps + recover.
  - `WEEKEND_SUN_PROMPT` — the current `SATURDAY_PROMPT` body (research-pipeline audit + recovery: `saturday_brain_finisher.js`, `saturday_brain_retry_failed.js`), since research now runs Sunday 08:00.
- Add both to `PROMPT_BY_MODE`: `'weekend-sat': WEEKEND_SAT_PROMPT, 'weekend-sun': WEEKEND_SUN_PROMPT,`

- [ ] **Step 5: Verify the new modes resolve**

Run:
```bash
cd /root/.config/superpowers/worktrees/weekend-process-overhaul
node --check src/agent/run_maintenance.js
node -e "const m=require('./src/agent/run_maintenance.js')" 2>/dev/null || true
node src/agent/run_maintenance.js --mode weekend-sat --help 2>&1 | head -3 || true
```
Expected: `node --check` valid. (If the script auto-runs on require, rely on `node --check` + a grep that both modes appear in `PROMPT_BY_MODE`.)
Also: `grep -c "weekend-sat\|weekend-sun" src/agent/run_maintenance.js` → expect ≥ 6 (3 dicts × 2 modes).

- [ ] **Step 6: Commit**

```bash
git add src/maintenance/weekend_saturday.sh tests/test_weekend_saturday_dryrun.sh src/agent/run_maintenance.js
chmod +x src/maintenance/weekend_saturday.sh
git commit -m "feat(weekend): Saturday adjustment+backtest driver; weekend-sat/sun maintenance modes"
```

---

## Task 9: systemd units + decommission doc

**Files:**
- Create: `docs/openclaw-weekend-saturday.{service,timer}`, `docs/openclaw-weekend-maintenance-sat.{service,timer}`, `docs/openclaw-weekend-sunday.{service,timer}`, `docs/openclaw-weekend-maintenance-sun.{service,timer}`, `docs/weekend-schedule-migration.md`

**Context:** These are repo-tracked unit templates (installed to `/etc/systemd/system/` at deploy). Follow the existing `docs/openclaw-*.{service,timer}` convention (User=claudebot, WorkingDirectory=/root/openclaw, EnvironmentFile=/root/openclaw/.env, OnCalendar with `America/New_York`).

- [ ] **Step 1: Saturday pipeline timer + service**

`docs/openclaw-weekend-saturday.timer`:
```ini
[Unit]
Description=OpenClaw weekend — Saturday 08:00 ET strategy-adjustment + backtest pipeline

[Timer]
OnCalendar=Sat *-*-* 08:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```
`docs/openclaw-weekend-saturday.service`:
```ini
[Unit]
Description=OpenClaw weekend — Saturday adjustment+backtest pipeline (review->recs->coupling->refresh->weights->panels->universe)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/bin/bash /root/openclaw/src/maintenance/weekend_saturday.sh
StandardOutput=append:/var/log/openclaw/weekend_saturday.log
StandardError=append:/var/log/openclaw/weekend_saturday.log
TimeoutStartSec=28800

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Sunday research timer + service**

`docs/openclaw-weekend-sunday.timer`:
```ini
[Unit]
Description=OpenClaw weekend — Sunday 08:00 ET research run (saturday-brain)

[Timer]
OnCalendar=Sun *-*-* 08:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```
`docs/openclaw-weekend-sunday.service`:
```ini
[Unit]
Description=OpenClaw weekend — Sunday research (8-phase saturday-brain pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node /root/openclaw/src/agent/curators/run_mastermind.js --mode saturday-brain
StandardOutput=append:/var/log/openclaw/weekend_sunday.log
StandardError=append:/var/log/openclaw/weekend_sunday.log
TimeoutStartSec=21600

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Saturday + Sunday 20:00 ET maintenance units**

`docs/openclaw-weekend-maintenance-sat.timer`:
```ini
[Unit]
Description=OpenClaw weekend — Saturday 20:00 ET maintenance (audit adjustment+backtest pipeline)

[Timer]
OnCalendar=Sat *-*-* 20:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```
`docs/openclaw-weekend-maintenance-sat.service`:
```ini
[Unit]
Description=OpenClaw weekend — Saturday maintenance (--mode weekend-sat)
After=network-online.target postgresql.service openclaw-weekend-saturday.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node /root/openclaw/src/agent/run_maintenance.js --mode weekend-sat
StandardOutput=append:/var/log/openclaw/weekend_maint_sat.log
StandardError=append:/var/log/openclaw/weekend_maint_sat.log
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
```
`docs/openclaw-weekend-maintenance-sun.timer`:
```ini
[Unit]
Description=OpenClaw weekend — Sunday 20:00 ET maintenance (audit research run)

[Timer]
OnCalendar=Sun *-*-* 20:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```
`docs/openclaw-weekend-maintenance-sun.service`:
```ini
[Unit]
Description=OpenClaw weekend — Sunday maintenance (--mode weekend-sun; audits research)
After=network-online.target postgresql.service openclaw-weekend-sunday.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node /root/openclaw/src/agent/run_maintenance.js --mode weekend-sun
StandardOutput=append:/var/log/openclaw/weekend_maint_sun.log
StandardError=append:/var/log/openclaw/weekend_maint_sun.log
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Decommission doc**

`docs/weekend-schedule-migration.md`:
```markdown
# Weekend schedule migration (2026-05-29)

Replaces the scattered weekend timers with four units. Old units are
`systemctl disable --now`'d (NOT deleted) so they can be re-enabled if needed.

## New units (install + enable at deploy)
| Unit | When (ET) | Runs |
|------|-----------|------|
| openclaw-weekend-saturday | Sat 08:00 | weekend_saturday.sh (review→critique→position-recs→coupling→backtest refresh→weights→panels→universe-recs) |
| openclaw-weekend-maintenance-sat | Sat 20:00 | run_maintenance.js --mode weekend-sat |
| openclaw-weekend-sunday | Sun 08:00 | run_mastermind.js --mode saturday-brain (research) |
| openclaw-weekend-maintenance-sun | Sun 20:00 | run_maintenance.js --mode weekend-sun (audits research) |

## Disabled (superseded; folded into the new units)
openclaw-mastermind-corpus, openclaw-paper-expansion, openclaw-backtest-refresh,
openclaw-strategy-backtest-refresh, openclaw-weekly-strategy-weights,
openclaw-strategy-review, openclaw-mastermind-critique, openclaw-position-recs,
openclaw-universe-recs, openclaw-botjohn-saturday-maintenance,
openclaw-botjohn-saturday-verify.

## Deploy commands (run on VPS)
    for u in openclaw-weekend-saturday openclaw-weekend-maintenance-sat \
             openclaw-weekend-sunday openclaw-weekend-maintenance-sun; do
      sudo cp docs/$u.service docs/$u.timer /etc/systemd/system/
    done
    sudo systemctl daemon-reload
    for u in openclaw-weekend-saturday openclaw-weekend-maintenance-sat \
             openclaw-weekend-sunday openclaw-weekend-maintenance-sun; do
      sudo systemctl enable --now $u.timer
    done
    for u in mastermind-corpus paper-expansion backtest-refresh \
             strategy-backtest-refresh weekly-strategy-weights strategy-review \
             mastermind-critique position-recs universe-recs \
             botjohn-saturday-maintenance botjohn-saturday-verify; do
      sudo systemctl disable --now openclaw-$u.timer || true
    done
```

- [ ] **Step 5: Validate the unit files parse**

Run (systemd-analyze may not be available in this env; the grep checks are the gate):
```bash
cd /root/.config/superpowers/worktrees/weekend-process-overhaul
for f in docs/openclaw-weekend-*.timer; do grep -q 'OnCalendar=' "$f" && echo "OK $f"; done
for f in docs/openclaw-weekend-*.service; do grep -q 'ExecStart=' "$f" && echo "OK $f"; done
```
Expected: `OK` for all 4 timers + 4 services.

- [ ] **Step 6: Commit**

```bash
git add docs/openclaw-weekend-*.service docs/openclaw-weekend-*.timer docs/weekend-schedule-migration.md
git commit -m "feat(weekend): four-slot weekend systemd units + decommission doc"
```

---

## Self-review checklist (already run by plan author)

- **Spec coverage:** A (T5,T6,T7), B (T8 step 7), C (T8,T9), D (T2,T3), E (T1 cols + T4). All mapped.
- **Type consistency:** `apply_override`/`resolve_override` signatures identical across T5/T6; `set_params` new kwargs match T1↔T7; `param_override` map shape `{regime:{stop_pct,target_pct}}` identical T5↔T7; audit columns `bt_sharpe_before/after`,`bt_n_trades` identical T1↔T7↔T4.
- **Gate consistency:** `OPENCLAW_BACKTEST_COUPLED_RECS` read only via `regime_param_override.gate_on()` / `backtest_coupled_recs.gate_on()`.

## Final review + deploy (after all tasks)

1. Run the full new test set: `PYTHONPATH=src python3 -m pytest tests/test_regime_param_override.py tests/test_backtest_coupled_recs.py tests/test_param_change_backtest_cols.py tests/test_pipeline_step_routing.py -v` + the node smokes.
2. Dispatch a final whole-branch code review (subagent-driven-development final reviewer).
3. **Pre-flip dry-run:** `OPENCLAW_BACKTEST_COUPLED_RECS=1 python3 -m execution.backtest_coupled_recs --dry-run` on the VPS — confirm it logs ΔSharpe per rec and would-apply/reject without writing.
4. Merge order: this branch stacks on PR #13 — coordinate with the operator (PR #13 + this either merge together or this rebases post-#13-merge).
5. Deploy: VPS `git pull` → run migration 125 → install/enable 4 new timers + disable 11 old (per `docs/weekend-schedule-migration.md`) → set `OPENCLAW_BACKTEST_COUPLED_RECS=1` in `/root/openclaw/.env` → restart `johnbot`.
6. Verify gate: `grep OPENCLAW_BACKTEST_COUPLED_RECS /root/openclaw/.env` and confirm timers: `systemctl list-timers 'openclaw-weekend-*'`.
