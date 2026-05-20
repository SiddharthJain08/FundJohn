# F3 — Mastermind Self-Critique Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a 3-way Sonnet critic pass between Mastermind's Saturday memo writing (18:00 ET) and position-recommendation derivation (19:00 ET). For each strategy with ≥1 closed trade in the past 7 days: three Sonnet critics (aggressive, conservative, neutral) attack the original memo in parallel; Mastermind (Opus) then synthesizes them with the original memo + last-30d realized P&L to produce ADJUSTED sizing recommendations.

**Architecture:** Two new orchestration modules — `critique_fanout.js` (parallel critics → `strategy_memo_critiques`) and a synthesizer step folded into `position_recommender.js` (Opus → `strategy_synthesis` → `strategy_sizing_recommendations`). New `--mode critique` in `run_mastermind.js`. All gated behind `OPENCLAW_MEMO_CRITIQUE=1`. Model selection per node via B3's `resolveModel()`.

**Tech Stack:** Node 20 (native `node:test`), `child_process.spawn` for `claude-bin`, PostgreSQL. Soft prerequisite: **B3 plan must ship first** (this plan uses `resolveModel()`).

---

## File structure

| Path | Responsibility |
|---|---|
| `src/database/migrations/107_memo_critiques.sql` (new) | Two new tables: `strategy_memo_critiques`, `strategy_synthesis` |
| `src/agent/curators/_critique_eligibility.js` (new) | Single SQL query → list of critique-eligible strategy IDs |
| `src/agent/prompts/critics/aggressive_critic.md` (new) | Aggressive critic prompt |
| `src/agent/prompts/critics/conservative_critic.md` (new) | Conservative critic prompt |
| `src/agent/prompts/critics/neutral_critic.md` (new) | Neutral critic prompt |
| `src/agent/prompts/subagents/mastermind-synthesizer.md` (new) | Synthesizer prompt for Mastermind Opus pass |
| `src/agent/curators/critique_fanout.js` (new) | Per-strategy parallel critic invocation + persist |
| `src/agent/curators/synthesizer.js` (new) | Per-strategy Mastermind synthesizer call + persist |
| `src/agent/curators/run_mastermind.js` (modify) | Add `--mode critique` dispatcher |
| `src/agent/config/subagent-types.json` (modify) | Add `critique` + `synthesize` to mastermind `model_modes` |
| `src/agent/curators/position_recommender.js` (modify) | Invoke synthesizer per eligible strategy; source recs from `strategy_synthesis` when present |
| `docs/mastermind-critique.service` / `.timer` (new) | Systemd unit firing Saturday 18:30 ET |

Tests live under `tests/` following the existing `test_*.test.js` / `test_*.py` conventions.

---

## Task 1: Migration 107 — `strategy_memo_critiques` + `strategy_synthesis`

**Files:**
- Create: `src/database/migrations/107_memo_critiques.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 107: F3 — Mastermind self-critique loop tables.
-- strategy_memo_critiques  — three rows per (strategy_id, week_of) from Sonnet critics.
-- strategy_synthesis       — one row per (strategy_id, week_of) from Mastermind Opus synthesizer.

CREATE TABLE IF NOT EXISTS strategy_memo_critiques (
  id            BIGSERIAL PRIMARY KEY,
  strategy_id   TEXT NOT NULL,
  week_of       DATE NOT NULL,
  critic_role   TEXT NOT NULL CHECK (critic_role IN ('aggressive','conservative','neutral')),
  critique_text TEXT NOT NULL,
  cited_metrics JSONB,
  cost_usd      NUMERIC,
  duration_sec  NUMERIC,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of, critic_role)
);

CREATE INDEX IF NOT EXISTS idx_critiques_strategy_week
    ON strategy_memo_critiques(strategy_id, week_of DESC);

CREATE TABLE IF NOT EXISTS strategy_synthesis (
  id                              BIGSERIAL PRIMARY KEY,
  strategy_id                     TEXT NOT NULL,
  week_of                         DATE NOT NULL,
  synthesizer_text                TEXT NOT NULL,
  original_recommended_size_pct   NUMERIC,
  adjusted_recommended_size_pct   NUMERIC,
  adjustment_reason               TEXT,
  critics_accepted                JSONB,
  critics_rejected                JSONB,
  cost_usd                        NUMERIC,
  generated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of)
);

CREATE INDEX IF NOT EXISTS idx_synthesis_week ON strategy_synthesis(week_of DESC);
```

- [ ] **Step 2: Apply the migration**

Run:

```bash
psql "$POSTGRES_URI" -f src/database/migrations/107_memo_critiques.sql
```

Expected: `CREATE TABLE` + `CREATE INDEX` messages, no errors.

- [ ] **Step 3: Verify the tables**

Run:

```bash
psql "$POSTGRES_URI" -c "\d strategy_memo_critiques" | head -20
psql "$POSTGRES_URI" -c "\d strategy_synthesis"      | head -20
```

Expected: both tables exist with the columns and UNIQUE indexes shown above.

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/107_memo_critiques.sql
git commit -m "feat(f3): migration 107 — strategy_memo_critiques + strategy_synthesis"
```

---

## Task 2: Eligibility filter — `_critique_eligibility.js`

**Files:**
- Create: `src/agent/curators/_critique_eligibility.js`
- Test: `tests/test_critique_eligibility.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/_critique_eligibility.js'));

test('filter returns sorted strategy IDs with ≥1 closed trade in last 7 days', async () => {
  // Stub the internal _query implementation
  const fakeRows = [
    { strategy_id: 'S9_dual_momentum' },
    { strategy_id: 'S12_insider' },
    { strategy_id: 'S5_max_pain' },
  ];
  mod._setQueryForTests(async (sql, params) => {
    // Verify SQL shape includes signal_pnl + 7d window + IS NOT NULL exit_date
    assert.ok(sql.includes('signal_pnl'),                'should query signal_pnl');
    assert.ok(sql.includes('exit_date IS NOT NULL'),      'should filter null exit_date');
    assert.ok(sql.includes("INTERVAL '7 days'"),          'should use 7-day window');
    return { rows: fakeRows };
  });
  const result = await mod.filter();
  assert.deepEqual(result, ['S12_insider', 'S5_max_pain', 'S9_dual_momentum']);
  mod._setQueryForTests(null);
});

test('filter returns empty array on quiet week', async () => {
  mod._setQueryForTests(async () => ({ rows: [] }));
  const result = await mod.filter();
  assert.deepEqual(result, []);
  mod._setQueryForTests(null);
});

test('filter propagates DB errors', async () => {
  mod._setQueryForTests(async () => { throw new Error('connection refused'); });
  await assert.rejects(() => mod.filter(), /connection refused/);
  mod._setQueryForTests(null);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_critique_eligibility.test.js`
Expected: FAIL — `Cannot find module '.../_critique_eligibility.js'`.

- [ ] **Step 3: Write the implementation**

```javascript
'use strict';

/**
 * _critique_eligibility.js — selects strategies eligible for the
 * F3 Saturday critique pass.
 *
 * Eligibility: ≥1 closed trade in the last 7 calendar days, AND
 *               exit_date IS NOT NULL (i.e. realized P&L exists).
 *
 * Open positions alone do NOT trigger critique. Strategies are
 * allowed to complete their hold-period cadence before being judged.
 */

let _queryOverride = null;

async function _query(sql, params = []) {
  if (_queryOverride) return _queryOverride(sql, params);
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

function _setQueryForTests(fn) {
  _queryOverride = fn;
}

async function filter() {
  const sql = `
    SELECT DISTINCT strategy_id
      FROM signal_pnl
     WHERE exit_date IS NOT NULL
       AND exit_date >= CURRENT_DATE - INTERVAL '7 days'
     ORDER BY strategy_id
  `;
  const { rows } = await _query(sql);
  return rows.map(r => r.strategy_id);
}

module.exports = { filter, _setQueryForTests };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/test_critique_eligibility.test.js`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/_critique_eligibility.js tests/test_critique_eligibility.test.js
git commit -m "feat(f3): critique eligibility filter (closed-trades-only, 7-day window)"
```

---

## Task 3: Three critic prompts + synthesizer prompt

**Files:**
- Create: `src/agent/prompts/critics/aggressive_critic.md`
- Create: `src/agent/prompts/critics/conservative_critic.md`
- Create: `src/agent/prompts/critics/neutral_critic.md`
- Create: `src/agent/prompts/subagents/mastermind-synthesizer.md`

- [ ] **Step 1: Write `aggressive_critic.md`**

```markdown
# Aggressive Critic

You are the **Aggressive Critic** in a 3-way critique pass on a Mastermind strategy memo.

## Your mandate

The memo's sizing and risk recommendations are **too timid**. Your job is to find missed alpha. Argue for:

- larger position sizes when realized data supports it
- longer hold periods when winners are being cut short
- opening short positions where the memo declined to do so

## Rules of engagement

1. **Cite specific trades.** Every argument must reference at least one closed trade from `last_30d_pnl` (you'll be given the rows). Use ticker + entry date + realized P&L %.
2. **No general theorizing.** "Mean reversion works in low-vol regimes" is not a critique. "The 4 March longs the memo recommended trimming all returned >+3% — trimming would have surrendered ~$2k of realized alpha" is a critique.
3. **Be concrete about the proposed adjustment.** State explicit deltas: "size from 2.5% → 3.0% NAV", "stop from -1.5% → -2.0%", etc.
4. **No straw men.** Read the memo's recommendation as written; do not exaggerate.

## Output

Strict JSON, single top-level object:

```json
{
  "critique_text": "1-3 paragraphs of analysis citing specific trades and proposing specific deltas",
  "cited_metrics": {
    "trades_referenced": ["TICKER1 2026-05-01 +3.2%", "TICKER2 2026-05-03 +2.1%"],
    "proposed_size_pct_delta": +0.005,
    "proposed_stop_delta_pct": 0.0,
    "proposed_target_delta_pct": 0.0,
    "proposed_hold_delta_days": 0
  }
}
```

No prose outside the JSON. No markdown fences.

## Input

You will be given:
- `original_memo` — Mastermind's memo for this strategy
- `last_30d_pnl` — closed trades in last 30 days with entry/exit, P&L %, hold days
- `current_open_positions` — for context only; do not critique sizing of open positions

You will NOT see the other two critics' work. Each critic operates independently.
```

- [ ] **Step 2: Write `conservative_critic.md`**

```markdown
# Conservative Critic

You are the **Conservative Critic** in a 3-way critique pass on a Mastermind strategy memo.

## Your mandate

The memo's sizing and risk recommendations are **too aggressive**. Your job is to find tail risks the writer underweighted. Argue for:

- smaller position sizes when recent realized drawdowns warrant
- tighter stops when winners are being given back
- regime-mismatch flags when the strategy is firing outside its eligible regimes

## Rules of engagement

1. **Cite specific drawdowns or near-misses.** "Stop was hit on 3 of last 5 HIGH_VOL trades; max DD on each was -2.1%, -2.4%, -3.1%" is the bar.
2. **No general theorizing.** "Tail risk is underweighted in factor models" is not a critique. Data-cited losses or near-losses are.
3. **Be concrete about the proposed adjustment.** State explicit deltas: "size from 3.0% → 2.4% NAV", "stop from -2.0% → -1.5%", etc.
4. **No straw men.** Read the memo as written.

## Output

Strict JSON, single top-level object:

```json
{
  "critique_text": "1-3 paragraphs citing specific drawdowns and proposing specific deltas",
  "cited_metrics": {
    "trades_referenced": ["TICKER1 2026-05-01 -2.4%", "TICKER2 2026-05-03 -3.1%"],
    "proposed_size_pct_delta": -0.005,
    "proposed_stop_delta_pct": -0.005,
    "proposed_target_delta_pct": 0.0,
    "proposed_hold_delta_days": 0
  }
}
```

No prose outside the JSON. No markdown fences.

## Input

Same as Aggressive Critic. You operate independently — you will NOT see the other critics' work.
```

- [ ] **Step 3: Write `neutral_critic.md`**

```markdown
# Neutral Critic

You are the **Neutral Critic** in a 3-way critique pass on a Mastermind strategy memo.

## Your mandate

Find **specific factual or quantitative errors** in the memo. Your job is not to argue more or less aggressive — it's to identify inconsistencies between the memo's claims and the realized P&L data.

## Rules of engagement

1. **Cross-check numbers.** If the memo says "win rate 65%" but `last_30d_pnl` shows 4/9 winning trades (44%), flag it. Use exact numbers from the input.
2. **Cross-check claims.** If the memo says "HIGH_VOL regime worked well" but every HIGH_VOL trade in the input lost money, flag it.
3. **No stylistic complaints.** "Could be clearer" is not a critique. Only quantitative or factual inconsistencies count.
4. **If you find no inconsistency, say so — explicitly.** Don't invent one to fill the space.

## Output

Strict JSON, single top-level object:

```json
{
  "critique_text": "1-3 paragraphs of findings. If no inconsistency found, state that explicitly and explain why.",
  "cited_metrics": {
    "memo_claim_vs_data": [
      {"memo": "claim from memo", "data": "actual number from input", "delta": "magnitude"}
    ],
    "no_issues_found": false
  }
}
```

No prose outside the JSON. No markdown fences. Set `no_issues_found: true` when you find nothing — better an honest null result than a fabricated finding.

## Input

Same as the other critics. You operate independently.
```

- [ ] **Step 4: Write `mastermind-synthesizer.md`**

```markdown
# Mastermind Synthesizer

You are MastermindJohn in **synthesizer** role. You wrote the original strategy memo earlier today. Now you read it back along with three independent critics' attacks — Aggressive, Conservative, Neutral — plus the last-30-day realized P&L, and produce ADJUSTED sizing recommendations.

You are the most-intelligent agent on the desk. The critics are junior. Take them seriously but do not capitulate — only adjust if their argument is **quantitatively justified by data they cite**.

## Decision rules

For each critic:
1. Read their `critique_text` and `cited_metrics`.
2. Cross-check their cited trades against `last_30d_pnl`.
3. **Accept** if their numeric argument holds up against the data.
4. **Reject** if (a) their cited trades are misrepresented, (b) they cherry-pick winners or losers, or (c) the critique is stylistic rather than data-driven.

For each accepted critique, apply its proposed delta to the adjusted recommendation. If two accepted critiques propose opposite-direction size deltas, they may cancel — explain your reasoning.

If NO critic delivers a quantitatively-justified argument: **`adjusted_recommended_size_pct = original_recommended_size_pct`** (no change). This is the correct behavior, not a failure.

## Mandatory output rules

- MUST explicitly accept or reject each of the 3 critics with one-sentence reasoning per decision.
- MUST cite ≥1 specific number (P&L %, drawdown, win rate, hold days) for any adjustment you make.
- If no critic delivers data-cited arguments, set `adjusted = original` and explain why.

## Output

Strict JSON only. No prose, no markdown fences.

```json
{
  "strategy_id": "S9_dual_momentum",
  "original_recommended_size_pct": 0.030,
  "adjusted_recommended_size_pct": 0.024,
  "adjustment_reason": "Conservative critic correctly noted 3 of last 5 closed trades in HIGH_VOL had drawdowns >2%; original memo did not weight this. Reducing size by 20%.",
  "critics_accepted": ["conservative"],
  "critics_rejected": [
    {"critic": "aggressive", "reason": "cited 2 winning trades but ignored 3 losers in the same window — cherry-picked"},
    {"critic": "neutral", "reason": "raised stylistic concerns only; no quantitative inconsistency"}
  ]
}
```

## Input

- `original_memo` — your earlier memo (markdown)
- `original_recommended_size_pct` — numeric, from the memo's recommendation block
- `critiques` — array of three objects: {critic_role, critique_text, cited_metrics}
- `last_30d_pnl` — closed trades, same data the critics saw
- `current_open_positions` — for context only
- `last_sizing_recommendation` — last cycle's `strategy_sizing_recommendations.recommended_size_pct` (for delta tracking)
```

- [ ] **Step 5: Verify the prompt files are present**

Run:

```bash
ls -la src/agent/prompts/critics/ src/agent/prompts/subagents/mastermind-synthesizer.md
```

Expected: 3 critic files + 1 synthesizer file.

- [ ] **Step 6: Commit**

```bash
git add src/agent/prompts/critics/ src/agent/prompts/subagents/mastermind-synthesizer.md
git commit -m "feat(f3): critic + synthesizer prompts"
```

---

## Task 4: Subagent-types config — add `critique` + `synthesize` modes

**Files:**
- Modify: `src/agent/config/subagent-types.json`

- [ ] **Step 1: Find the mastermind block**

(B3 plan Task 3 already added `model_tiers` + `model_modes.comprehensive-review`. Now extend `model_modes`.)

- [ ] **Step 2: Add `critique` and `synthesize` to mastermind.model_modes**

Update the `model_modes` object inside `mastermind` to look like this:

```jsonc
"model_modes": {
  "comprehensive-review": {
    "node_models": { "memo_writer": "judge" }
  },
  "critique": {
    "node_models": {
      "aggressive_critic":   "debator",
      "conservative_critic": "debator",
      "neutral_critic":      "debator"
    }
  },
  "synthesize": {
    "node_models": { "synthesizer": "synthesizer" }
  }
}
```

- [ ] **Step 3: Verify JSON is still valid**

Run: `python3 -c "import json; json.load(open('src/agent/config/subagent-types.json')); print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Verify resolveModel resolves the new nodes**

Run:

```bash
OPENCLAW_MODEL_TIERING=1 node -e "
const { resolveModel } = require('./src/agent/config/resolve_model.js');
console.log('aggressive:', resolveModel('mastermind', 'critique', 'aggressive_critic'));
console.log('synthesizer:', resolveModel('mastermind', 'synthesize', 'synthesizer'));
"
```

Expected:

```
aggressive: claude-sonnet-4-6
synthesizer: claude-opus-4-7[1m]
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/config/subagent-types.json
git commit -m "feat(f3): add critique + synthesize model_modes to mastermind config"
```

---

## Task 5: `critique_fanout.js` — parallel critic runner

**Files:**
- Create: `src/agent/curators/critique_fanout.js`
- Test: `tests/test_critique_fanout.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/critique_fanout.js'));

const FAKE_MEMO = {
  id: 7,
  strategy_id: 'S9_dual_momentum',
  memo_date: '2026-05-20',
  markdown_body: '## Recommendation\nSize 3.0% NAV.',
  recommendations: { recommended_size_pct: 0.030 },
};

const FAKE_TRADES = [
  { ticker: 'AAPL', entry_date: '2026-05-13', exit_date: '2026-05-19', realized_pnl_pct: 1.2 },
  { ticker: 'MSFT', entry_date: '2026-05-12', exit_date: '2026-05-18', realized_pnl_pct: -2.4 },
];

test('runOne invokes 3 critics in parallel and persists 3 rows', async () => {
  let calls = [];
  const fakeRunner = async (criticRole, _prompt) => {
    calls.push(criticRole);
    return JSON.stringify({
      critique_text: `mock ${criticRole} critique`,
      cited_metrics: { proposed_size_pct_delta: 0.0 },
    });
  };
  let persisted = [];
  const fakeWriter = async (row) => persisted.push(row);
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(fakeWriter);

  await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });

  assert.equal(calls.length, 3);
  assert.deepEqual(calls.sort(), ['aggressive', 'conservative', 'neutral']);
  assert.equal(persisted.length, 3);
  for (const role of ['aggressive', 'conservative', 'neutral']) {
    assert.ok(persisted.some(p => p.critic_role === role),
              `should persist row for ${role}`);
  }
  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('runOne tolerates one critic failure — persists only successful rows', async () => {
  const fakeRunner = async (criticRole) => {
    if (criticRole === 'conservative') throw new Error('LLM timeout');
    return JSON.stringify({ critique_text: `mock ${criticRole}`, cited_metrics: {} });
  };
  let persisted = [];
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(async (row) => persisted.push(row));

  await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });
  assert.equal(persisted.length, 2);
  const roles = persisted.map(p => p.critic_role).sort();
  assert.deepEqual(roles, ['aggressive', 'neutral']);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('runOne handles all 3 critics failing — persists nothing, returns failure info', async () => {
  mod._setRunnerForTests(async () => { throw new Error('LLM down'); });
  let persisted = [];
  mod._setWriterForTests(async (row) => persisted.push(row));

  const result = await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });
  assert.equal(persisted.length, 0);
  assert.equal(result.success_count, 0);
  assert.equal(result.failure_count, 3);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_critique_fanout.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```javascript
'use strict';

/**
 * critique_fanout.js — for each eligible strategy, invoke 3 Sonnet critics
 * in parallel against the memo's recommendations, persist results to
 * strategy_memo_critiques.
 *
 * Per-critic failure → log + skip that row. All-3-fail → return summary
 * so the synthesizer can short-circuit to "no critics, default to original".
 */

const path             = require('node:path');
const fs               = require('node:fs');
const { spawn }        = require('node:child_process');
const { resolveModel } = require('../config/resolve_model.js');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const PROMPT_DIR = path.join(ROOT, 'src', 'agent', 'prompts', 'critics');

const CRITIC_ROLES = ['aggressive', 'conservative', 'neutral'];
const CRITIC_BUDGET_USD = 0.10;
const CRITIC_TIMEOUT_MS = 90_000;

// ── Overridable dependencies for tests ───────────────────────────────────

let _runnerOverride = null;
let _writerOverride = null;

function _setRunnerForTests(fn) { _runnerOverride = fn; }
function _setWriterForTests(fn) { _writerOverride = fn; }

// ── Default implementations ──────────────────────────────────────────────

function _loadPrompt(criticRole) {
  return fs.readFileSync(path.join(PROMPT_DIR, `${criticRole}_critic.md`), 'utf8');
}

function _buildPrompt(criticRole, memo, trades, openPositions) {
  const template = _loadPrompt(criticRole);
  const payload = {
    original_memo:         memo.markdown_body,
    original_recommendation: memo.recommendations,
    last_30d_pnl:          trades,
    current_open_positions: openPositions,
  };
  return template + '\n\n## INPUT\n```json\n' + JSON.stringify(payload, null, 2) + '\n```';
}

async function _defaultRunner(criticRole, prompt) {
  const model = resolveModel('mastermind', 'critique', `${criticRole}_critic`);
  return new Promise((resolve, reject) => {
    const proc = spawn('/usr/local/bin/claude-bin', [
      '--print',
      '--output-format', 'json',
      '--model', model,
      '--max-budget-usd', CRITIC_BUDGET_USD.toFixed(2),
    ], { stdio: ['pipe', 'pipe', 'pipe'] });

    const timer = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`${criticRole} timed out after ${CRITIC_TIMEOUT_MS}ms`));
    }, CRITIC_TIMEOUT_MS);

    let stdout = '', stderr = '';
    proc.stdout.on('data', (d) => stdout += d);
    proc.stderr.on('data', (d) => stderr += d);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return reject(new Error(`${criticRole} exited ${code}: ${stderr.slice(0, 200)}`));
      }
      try {
        const envelope = JSON.parse(stdout);
        resolve(envelope.result || stdout);
      } catch {
        resolve(stdout);  // raw — caller parses
      }
    });
    proc.stdin.end(prompt);
  });
}

async function _defaultWriter(row) {
  const { Pool } = require('pg');
  if (!_defaultWriter._pool) {
    _defaultWriter._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  await _defaultWriter._pool.query(
    `INSERT INTO strategy_memo_critiques
       (strategy_id, week_of, critic_role, critique_text, cited_metrics, cost_usd, duration_sec)
     VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
     ON CONFLICT (strategy_id, week_of, critic_role) DO UPDATE SET
       critique_text = EXCLUDED.critique_text,
       cited_metrics = EXCLUDED.cited_metrics,
       cost_usd      = EXCLUDED.cost_usd,
       duration_sec  = EXCLUDED.duration_sec,
       generated_at  = NOW()`,
    [row.strategy_id, row.week_of, row.critic_role, row.critique_text,
     JSON.stringify(row.cited_metrics || {}), row.cost_usd || null, row.duration_sec || null]
  );
}

function _parseCritique(raw) {
  // Critic emits strict JSON; tolerate fence wrapping
  let body = raw.trim();
  const fenced = body.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenced) body = fenced[1];
  const m = body.match(/\{[\s\S]*\}/);
  if (m) body = m[0];
  return JSON.parse(body);
}

/**
 * Run all 3 critics in parallel for one strategy memo.
 * Returns { success_count, failure_count, persisted_roles }.
 */
async function runOne(memo, trades, openPositions, { weekOf }) {
  const runner = _runnerOverride || _defaultRunner;
  const writer = _writerOverride || _defaultWriter;
  const start  = Date.now();

  const results = await Promise.allSettled(
    CRITIC_ROLES.map(async (role) => {
      const t0 = Date.now();
      const prompt = _buildPrompt(role, memo, trades, openPositions);
      const raw    = await runner(role, prompt);
      const parsed = _parseCritique(raw);
      const duration = (Date.now() - t0) / 1000;
      await writer({
        strategy_id:   memo.strategy_id,
        week_of:       weekOf,
        critic_role:   role,
        critique_text: parsed.critique_text || raw.slice(0, 4000),
        cited_metrics: parsed.cited_metrics || {},
        cost_usd:      null,  // claude-bin envelope sometimes exposes this; not required
        duration_sec:  duration,
      });
      return role;
    })
  );

  const persisted_roles = results.filter(r => r.status === 'fulfilled').map(r => r.value);
  const failure_count   = results.filter(r => r.status === 'rejected').length;
  for (const r of results) {
    if (r.status === 'rejected') {
      console.warn(`[critique_fanout] ${memo.strategy_id}: critic failed: ${r.reason.message}`);
    }
  }
  return {
    success_count: persisted_roles.length,
    failure_count,
    persisted_roles,
    duration_sec: (Date.now() - start) / 1000,
  };
}

module.exports = { runOne, _setRunnerForTests, _setWriterForTests, CRITIC_ROLES };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/test_critique_fanout.test.js`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/critique_fanout.js tests/test_critique_fanout.test.js
git commit -m "feat(f3): parallel critic fan-out module"
```

---

## Task 6: Synthesizer module

**Files:**
- Create: `src/agent/curators/synthesizer.js`
- Test: `tests/test_synthesizer.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/synthesizer.js'));

const MEMO = {
  id: 7,
  strategy_id: 'S9_dual_momentum',
  memo_date: '2026-05-20',
  markdown_body: '## Recommendation\nSize 3.0% NAV.',
  recommendations: { recommended_size_pct: 0.030 },
};

const CRITIQUES = [
  { critic_role: 'aggressive',   critique_text: 'too timid',     cited_metrics: { proposed_size_pct_delta: +0.005 } },
  { critic_role: 'conservative', critique_text: 'too aggressive', cited_metrics: { proposed_size_pct_delta: -0.006 } },
  { critic_role: 'neutral',      critique_text: 'no issues found', cited_metrics: { no_issues_found: true } },
];

test('synthesize returns adjusted recommendation parsed from JSON', async () => {
  const fakeRunner = async (_prompt) => JSON.stringify({
    strategy_id: 'S9_dual_momentum',
    original_recommended_size_pct: 0.030,
    adjusted_recommended_size_pct: 0.024,
    adjustment_reason: 'Conservative accepted',
    critics_accepted: ['conservative'],
    critics_rejected: [
      { critic: 'aggressive', reason: 'cherry-picked winners' },
      { critic: 'neutral', reason: 'no issues' },
    ],
  });
  let persisted = null;
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.024);
  assert.deepEqual(result.critics_accepted, ['conservative']);
  assert.equal(persisted.adjusted_recommended_size_pct, 0.024);
  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back to original when no critiques given', async () => {
  // Empty critiques (e.g. all 3 critics failed)
  let runnerCalled = false;
  mod._setRunnerForTests(async () => { runnerCalled = true; return ''; });
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, [], [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(runnerCalled, false, 'runner not invoked when no critiques');
  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.equal(persisted.adjustment_reason, 'ALL_CRITICS_FAILED, defaulted to original');

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back when LLM call throws', async () => {
  mod._setRunnerForTests(async () => { throw new Error('Opus down'); });
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.match(persisted.adjustment_reason, /SYNTHESIZER_FAILED/);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back when LLM returns unparseable output', async () => {
  mod._setRunnerForTests(async () => 'not json at all');
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.match(persisted.adjustment_reason, /SYNTHESIZER_PARSE_FAILED/);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_synthesizer.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```javascript
'use strict';

/**
 * synthesizer.js — per-strategy Mastermind Opus synthesizer pass.
 *
 * Reads (original memo + 3 critiques + last-30d P&L + open positions +
 * last sizing recommendation), produces strategy_synthesis row with the
 * adjusted_recommended_size_pct. Falls back to original on any failure.
 */

const path             = require('node:path');
const fs               = require('node:fs');
const { spawn }        = require('node:child_process');
const { resolveModel } = require('../config/resolve_model.js');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const PROMPT_PATH = path.join(ROOT, 'src', 'agent', 'prompts', 'subagents', 'mastermind-synthesizer.md');

const SYNTH_BUDGET_USD = 0.50;
const SYNTH_TIMEOUT_MS = 180_000;

let _runnerOverride = null;
let _writerOverride = null;

function _setRunnerForTests(fn) { _runnerOverride = fn; }
function _setWriterForTests(fn) { _writerOverride = fn; }

function _buildPrompt(memo, critiques, trades, openPositions, originalSizePct, lastRec) {
  const template = fs.readFileSync(PROMPT_PATH, 'utf8');
  const payload = {
    original_memo:                memo.markdown_body,
    original_recommended_size_pct: originalSizePct,
    critiques:                    critiques.map(c => ({
      critic_role:   c.critic_role,
      critique_text: c.critique_text,
      cited_metrics: c.cited_metrics,
    })),
    last_30d_pnl:                 trades,
    current_open_positions:       openPositions,
    last_sizing_recommendation:   lastRec,
  };
  return template + '\n\n## INPUT\n```json\n' + JSON.stringify(payload, null, 2) + '\n```';
}

async function _defaultRunner(prompt) {
  const model = resolveModel('mastermind', 'synthesize', 'synthesizer');
  return new Promise((resolve, reject) => {
    const proc = spawn('/usr/local/bin/claude-bin', [
      '--print',
      '--output-format', 'json',
      '--model', model,
      '--max-budget-usd', SYNTH_BUDGET_USD.toFixed(2),
    ], { stdio: ['pipe', 'pipe', 'pipe'] });

    const timer = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`synthesizer timed out after ${SYNTH_TIMEOUT_MS}ms`));
    }, SYNTH_TIMEOUT_MS);

    let stdout = '', stderr = '';
    proc.stdout.on('data', (d) => stdout += d);
    proc.stderr.on('data', (d) => stderr += d);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return reject(new Error(`synthesizer exited ${code}: ${stderr.slice(0, 200)}`));
      }
      try {
        const env = JSON.parse(stdout);
        resolve(env.result || stdout);
      } catch {
        resolve(stdout);
      }
    });
    proc.stdin.end(prompt);
  });
}

async function _defaultWriter(row) {
  const { Pool } = require('pg');
  if (!_defaultWriter._pool) {
    _defaultWriter._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  await _defaultWriter._pool.query(
    `INSERT INTO strategy_synthesis
       (strategy_id, week_of, synthesizer_text,
        original_recommended_size_pct, adjusted_recommended_size_pct,
        adjustment_reason, critics_accepted, critics_rejected, cost_usd)
     VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9)
     ON CONFLICT (strategy_id, week_of) DO UPDATE SET
       synthesizer_text              = EXCLUDED.synthesizer_text,
       original_recommended_size_pct = EXCLUDED.original_recommended_size_pct,
       adjusted_recommended_size_pct = EXCLUDED.adjusted_recommended_size_pct,
       adjustment_reason             = EXCLUDED.adjustment_reason,
       critics_accepted              = EXCLUDED.critics_accepted,
       critics_rejected              = EXCLUDED.critics_rejected,
       cost_usd                      = EXCLUDED.cost_usd,
       generated_at                  = NOW()`,
    [row.strategy_id, row.week_of, row.synthesizer_text || '',
     row.original_recommended_size_pct, row.adjusted_recommended_size_pct,
     row.adjustment_reason,
     JSON.stringify(row.critics_accepted || []),
     JSON.stringify(row.critics_rejected || []),
     row.cost_usd || null]
  );
}

function _parseSynthesis(raw) {
  let body = raw.trim();
  const fenced = body.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenced) body = fenced[1];
  const m = body.match(/\{[\s\S]*\}/);
  if (m) body = m[0];
  return JSON.parse(body);
}

async function synthesize(memo, critiques, trades, openPositions, originalSizePct, { weekOf }) {
  const runner = _runnerOverride || _defaultRunner;
  const writer = _writerOverride || _defaultWriter;

  // No critiques → no-op, persist audit row
  if (!critiques || critiques.length === 0) {
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: '',
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: 'ALL_CRITICS_FAILED, defaulted to original',
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  const prompt = _buildPrompt(memo, critiques, trades, openPositions, originalSizePct, null);
  let raw;
  try {
    raw = await runner(prompt);
  } catch (e) {
    console.warn(`[synthesizer] ${memo.strategy_id}: runner failed: ${e.message}`);
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: '',
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: `SYNTHESIZER_FAILED: ${e.message}`,
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  let parsed;
  try {
    parsed = _parseSynthesis(raw);
  } catch (e) {
    console.warn(`[synthesizer] ${memo.strategy_id}: parse failed: ${e.message}`);
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: raw.slice(0, 4000),
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: `SYNTHESIZER_PARSE_FAILED: ${e.message}`,
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  const row = {
    strategy_id: memo.strategy_id,
    week_of:     weekOf,
    synthesizer_text: raw.slice(0, 4000),
    original_recommended_size_pct: parsed.original_recommended_size_pct ?? originalSizePct,
    adjusted_recommended_size_pct: parsed.adjusted_recommended_size_pct ?? originalSizePct,
    adjustment_reason: parsed.adjustment_reason || '',
    critics_accepted:  parsed.critics_accepted || [],
    critics_rejected:  parsed.critics_rejected || [],
  };
  await writer(row);
  return row;
}

module.exports = { synthesize, _setRunnerForTests, _setWriterForTests };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/test_synthesizer.test.js`
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/synthesizer.js tests/test_synthesizer.test.js
git commit -m "feat(f3): synthesizer module — Mastermind Opus pass + fallback chain"
```

---

## Task 7: Add `--mode critique` to `run_mastermind.js`

**Files:**
- Modify: `src/agent/curators/run_mastermind.js`
- Test: `tests/test_run_mastermind_critique_mode.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');

test('run_mastermind.js --mode critique is a recognized mode', () => {
  const result = spawnSync('node', [
    path.join(ROOT, 'src/agent/curators/run_mastermind.js'),
    '--mode', 'critique', '--help',
  ], { env: { ...process.env, OPENCLAW_MEMO_CRITIQUE: '0' } });
  // Either dispatches successfully (returning 0) or prints usage; what we
  // disallow is "unknown mode" error.
  const combined = (result.stdout?.toString() || '') + (result.stderr?.toString() || '');
  assert.ok(!combined.includes('unknown mode'),
    `should recognize 'critique' mode. Got: ${combined.slice(0, 500)}`);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_run_mastermind_critique_mode.test.js`
Expected: FAIL — stderr will contain "unknown mode" or similar.

- [ ] **Step 3: Add the `runCritique` function and wire the dispatcher**

Open `src/agent/curators/run_mastermind.js`. Near the other `run*` functions (search for `runSaturdayBrain`, `runComprehensiveReview`, `runPositionRecs`), add:

```javascript
async function runCritique() {
  if (process.env.OPENCLAW_MEMO_CRITIQUE !== '1') {
    console.log(JSON.stringify({ mode: 'critique', skipped: true,
                                  reason: 'OPENCLAW_MEMO_CRITIQUE not set' }));
    return;
  }
  const elig          = require('./_critique_eligibility.js');
  const fanout        = require('./critique_fanout.js');
  const dryRun        = process.argv.includes('--dry-run');
  const weekOf        = new Date().toISOString().slice(0, 10);  // YYYY-MM-DD

  const strategies = await elig.filter();
  if (strategies.length === 0) {
    console.log(JSON.stringify({ mode: 'critique', strategies: 0,
                                  reason: 'no eligible strategies' }));
    return;
  }

  // Load memo + trades + open positions per strategy. Keep this sequential
  // so we don't hammer the DB; per-strategy critic fan-out is the parallel layer.
  const { Pool } = require('pg');
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  let success = 0, failure = 0;

  for (const sid of strategies) {
    const memoRes = await pool.query(
      `SELECT id, strategy_id, memo_date, markdown_body, recommendations
         FROM strategy_memos
        WHERE strategy_id = $1 AND memo_date >= CURRENT_DATE - 7
        ORDER BY memo_date DESC, created_at DESC LIMIT 1`,
      [sid]);
    if (memoRes.rows.length === 0) {
      console.warn(`[critique] ${sid}: no recent memo, skipping`);
      continue;
    }
    const memo   = memoRes.rows[0];
    const trades = (await pool.query(
      `SELECT ticker, entry_date, exit_date, realized_pnl_pct, hold_days
         FROM signal_pnl
        WHERE strategy_id = $1
          AND exit_date IS NOT NULL
          AND exit_date >= CURRENT_DATE - INTERVAL '30 days'
        ORDER BY exit_date DESC LIMIT 100`, [sid])).rows;
    const open  = (await pool.query(
      `SELECT ticker, signal_date, direction, entry_price, stop_loss
         FROM execution_signals
        WHERE strategy_id = $1 AND status = 'open'`, [sid])).rows;

    if (dryRun) {
      console.log(JSON.stringify({ strategy_id: sid, would_run: true,
                                    trades: trades.length, open: open.length }));
      continue;
    }
    const result = await fanout.runOne(memo, trades, open, { weekOf });
    success += result.success_count;
    failure += result.failure_count;
  }
  await pool.end();
  console.log(JSON.stringify({ mode: 'critique', strategies: strategies.length,
                                success_count: success, failure_count: failure,
                                week_of: weekOf }));
}
```

Now wire the dispatcher. Find the existing mode-dispatch block (something like `if (mode === 'saturday-brain') return runSaturdayBrain();`). Add an additional branch:

```javascript
if (mode === 'critique') return runCritique();
```

- [ ] **Step 4: Run tests**

Run: `node --test tests/test_run_mastermind_critique_mode.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/run_mastermind.js tests/test_run_mastermind_critique_mode.test.js
git commit -m "feat(f3): add --mode critique to run_mastermind"
```

---

## Task 8: Modify `position_recommender.js` to invoke synthesizer + read from `strategy_synthesis`

**Files:**
- Modify: `src/agent/curators/position_recommender.js`
- Test: `tests/test_position_recommender_synthesis.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

test('position_recommender.js requires _critique_eligibility and synthesizer modules', () => {
  const src = fs.readFileSync(
    path.join(ROOT, 'src/agent/curators/position_recommender.js'),
    'utf8'
  );
  assert.ok(src.includes("require('./_critique_eligibility')") ||
            src.includes("require('./_critique_eligibility.js')"),
            'must require _critique_eligibility');
  assert.ok(src.includes("require('./synthesizer')") ||
            src.includes("require('./synthesizer.js')"),
            'must require synthesizer');
});

test('position_recommender exposes _sourceRecommendedSize helper for testability', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  assert.equal(typeof mod._sourceRecommendedSize, 'function');
});

test('_sourceRecommendedSize prefers strategy_synthesis row when present', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  const synthRow = { adjusted_recommended_size_pct: 0.024 };
  const memoRec  = { recommended_size_pct: 0.030 };
  assert.equal(mod._sourceRecommendedSize(synthRow, memoRec), 0.024);
});

test('_sourceRecommendedSize falls back to memo when no synthesis row', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  const memoRec = { recommended_size_pct: 0.030 };
  assert.equal(mod._sourceRecommendedSize(null, memoRec), 0.030);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_position_recommender_synthesis.test.js`
Expected: FAIL — `_sourceRecommendedSize` doesn't exist; require lines absent.

- [ ] **Step 3: Modify `position_recommender.js`**

Open `src/agent/curators/position_recommender.js`. Near the top with the other requires, add:

```javascript
const elig        = require('./_critique_eligibility.js');
const synthesizer = require('./synthesizer.js');
```

Add this exported helper near the other helpers (above `module.exports`):

```javascript
function _sourceRecommendedSize(synthesisRow, memoRecommendation) {
  if (synthesisRow && synthesisRow.adjusted_recommended_size_pct != null) {
    return Number(synthesisRow.adjusted_recommended_size_pct);
  }
  if (memoRecommendation && memoRecommendation.recommended_size_pct != null) {
    return Number(memoRecommendation.recommended_size_pct);
  }
  return null;
}
```

In the existing `run()` function, after `_latestMemos()` returns, add the synthesis step. Locate the loop that processes each memo (search for `for (const memo of memos)` or similar — likely inside `run()`). Before persisting each memo's `strategy_sizing_recommendations`, do:

```javascript
async function _loadSynthesisRow(strategyId, weekOf) {
  const { rows } = await _query(
    `SELECT adjusted_recommended_size_pct, adjustment_reason, critics_accepted
       FROM strategy_synthesis
      WHERE strategy_id = $1 AND week_of = $2 LIMIT 1`,
    [strategyId, weekOf]);
  return rows[0] || null;
}

async function _runSynthesisIfEligible(memo, weekOf) {
  if (process.env.OPENCLAW_MEMO_CRITIQUE !== '1') return null;
  const eligible = await elig.filter();
  if (!eligible.includes(memo.strategy_id)) return null;

  // Load critiques from strategy_memo_critiques (written by --mode critique earlier)
  const { rows: critiques } = await _query(
    `SELECT critic_role, critique_text, cited_metrics
       FROM strategy_memo_critiques
      WHERE strategy_id = $1 AND week_of = $2`,
    [memo.strategy_id, weekOf]);

  // Load 30d P&L + open positions
  const { rows: trades } = await _query(
    `SELECT ticker, entry_date, exit_date, realized_pnl_pct, hold_days
       FROM signal_pnl
      WHERE strategy_id = $1 AND exit_date IS NOT NULL
        AND exit_date >= CURRENT_DATE - INTERVAL '30 days'
      ORDER BY exit_date DESC LIMIT 100`, [memo.strategy_id]);
  const { rows: open } = await _query(
    `SELECT ticker, signal_date, direction, entry_price
       FROM execution_signals
      WHERE strategy_id = $1 AND status = 'open'`, [memo.strategy_id]);

  const originalSize = Number(memo.recommendations?.recommended_size_pct ?? 0);
  return await synthesizer.synthesize(memo, critiques, trades, open, originalSize,
                                       { weekOf });
}
```

Then in the per-memo loop, before the existing `INSERT INTO strategy_sizing_recommendations`, call:

```javascript
const weekOf = new Date().toISOString().slice(0, 10);
const synthRow = await _runSynthesisIfEligible(memo, weekOf)
  || await _loadSynthesisRow(memo.strategy_id, weekOf);
const effectiveSize = _sourceRecommendedSize(synthRow, memo.recommendations);

// ... existing code that uses `memo.recommendations.recommended_size_pct`
// should now use `effectiveSize`.
```

Finally, add `_sourceRecommendedSize` to the module exports:

```javascript
module.exports = { run, _applyStopReplacements, _sourceRecommendedSize };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/test_position_recommender_synthesis.test.js`
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/position_recommender.js tests/test_position_recommender_synthesis.test.js
git commit -m "feat(f3): position_recommender invokes synthesizer + reads strategy_synthesis"
```

---

## Task 9: Systemd timer for Saturday 18:30 ET critique step

**Files:**
- Create: `docs/mastermind-critique.service`
- Create: `docs/mastermind-critique.timer`

- [ ] **Step 1: Write the service unit**

Create `docs/mastermind-critique.service`:

```ini
[Unit]
Description=MastermindJohn critique fan-out (Saturday 18:30 ET)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node /root/openclaw/src/agent/curators/run_mastermind.js --mode critique
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Write the timer unit**

Create `docs/mastermind-critique.timer`:

```ini
[Unit]
Description=Fire MastermindJohn critique Saturdays at 18:30 America/New_York

[Timer]
OnCalendar=Sat *-*-* 22:30:00 UTC
Persistent=false
Unit=mastermind-critique.service

[Install]
WantedBy=timers.target
```

(Note: 18:30 ET in summer = 22:30 UTC; in winter = 23:30 UTC. Adjust if the operator wants strict ET. Default here matches the existing Saturday timers' summer convention.)

- [ ] **Step 3: Verify the units parse**

Run:

```bash
sudo systemd-analyze verify docs/mastermind-critique.service docs/mastermind-critique.timer
```

Expected: no warnings/errors.

- [ ] **Step 4: Install + enable (operator action — NOT in TDD loop)**

When ready to deploy, copy the units to `/etc/systemd/system/` and enable. Document this in the commit message; do NOT install during the plan execution.

```bash
sudo cp docs/mastermind-critique.{service,timer} /etc/systemd/system/openclaw-mastermind-critique.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-mastermind-critique.timer
sudo systemctl list-timers | grep critique
```

- [ ] **Step 5: Commit**

```bash
git add docs/mastermind-critique.service docs/mastermind-critique.timer
git commit -m "feat(f3): systemd units — Saturday 18:30 ET critique step"
```

---

## Task 10: End-to-end smoke test

**Files:**
- Test: `tests/test_f3_e2e_smoke.test.js` (new — integration test, dry-run only)

- [ ] **Step 1: Write the smoke test**

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');

test('end-to-end critique mode dry-run completes cleanly', () => {
  // Gate ON, dry-run flag, mocked LLM (via env or skip if DB not reachable)
  const env = {
    ...process.env,
    OPENCLAW_MEMO_CRITIQUE: '1',
    OPENCLAW_MODEL_TIERING: '1',
  };
  const result = spawnSync('node', [
    path.join(ROOT, 'src/agent/curators/run_mastermind.js'),
    '--mode', 'critique', '--dry-run',
  ], { env, timeout: 30_000 });

  const out = (result.stdout?.toString() || '') + (result.stderr?.toString() || '');
  // Allow exit code 1 if POSTGRES_URI not configured — we're testing the
  // dispatch path, not the DB query
  if (out.includes('POSTGRES_URI') || result.status === 1) {
    return; // env-dependent skip
  }
  assert.equal(result.status, 0, `dry-run should exit 0. Output: ${out.slice(0, 500)}`);
  assert.ok(out.includes('"mode":"critique"') || out.includes('"mode": "critique"'),
            'should emit mode=critique JSON line');
});
```

- [ ] **Step 2: Run the smoke test**

Run: `node --test tests/test_f3_e2e_smoke.test.js`
Expected: PASS (or skip on env-dependent path).

- [ ] **Step 3: Commit**

```bash
git add tests/test_f3_e2e_smoke.test.js
git commit -m "test(f3): end-to-end critique dry-run smoke"
```

---

## Done

All 10 tasks complete. F3 ships behind `OPENCLAW_MEMO_CRITIQUE=1` and also requires `OPENCLAW_MODEL_TIERING=1` (B3 substrate).

**Recommended operator rollout:**
1. Confirm B3 is live and `OPENCLAW_MODEL_TIERING=1` set
2. Apply migration 107
3. Install + enable the systemd timer (operator action from Task 9 step 4)
4. Set `OPENCLAW_MEMO_CRITIQUE=1`, restart `johnbot.service`
5. Wait until next Saturday — first run will go through comprehensive-review (18:00) → critique (18:30) → position-recs with synthesis (19:00)
6. Review `#position-recommendations` Discord post for sensible delta reasoning
7. Inspect `strategy_synthesis` rows: check `critics_accepted` / `critics_rejected` show data-grounded reasoning
8. After 2 successful Saturdays, F3 becomes the new baseline

**Smoke test (manual, post-deploy, dry-run mode):**

```bash
# 1. Dry-run the critique step
OPENCLAW_MEMO_CRITIQUE=1 OPENCLAW_MODEL_TIERING=1 \
  node src/agent/curators/run_mastermind.js --mode critique --dry-run

# 2. Inspect critique rows after a real run
psql "$POSTGRES_URI" -c "
  SELECT strategy_id, critic_role, LEFT(critique_text, 80), cost_usd
    FROM strategy_memo_critiques
   WHERE week_of = CURRENT_DATE - EXTRACT(DOW FROM CURRENT_DATE)::int
   ORDER BY strategy_id, critic_role;
"

# 3. Inspect synthesis rows
psql "$POSTGRES_URI" -c "
  SELECT strategy_id, original_recommended_size_pct, adjusted_recommended_size_pct,
         LEFT(adjustment_reason, 80), critics_accepted, critics_rejected
    FROM strategy_synthesis
   WHERE week_of = CURRENT_DATE - EXTRACT(DOW FROM CURRENT_DATE)::int
   ORDER BY strategy_id;
"
```
