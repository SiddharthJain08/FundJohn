# SP-2 Phase C: Mastermind Universe-Recs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Saturday 20:00 ET, MastermindJohn (Opus 4.7 1M) evaluates each live strategy against the 12 candidate predicates from Phase A and proposes a switch when one materially out-performs the current choice. Operator approves via Discord reaction; adoption is atomic across DB + manifest + audit log.

**Architecture:** New mode `--mode universe-recs` on `run_mastermind.js`. Per strategy: build deterministic 12-candidate backtest grid via `regime_blended_backtest.py --resolver-override`; pack into Opus 4.7 1M prompt; parse strict-schema JSON; persist; post to `#universe-recs`. Operator reactions ✅/❌/⏸ route through `src/channels/discord/bot.js` → dashboard endpoints → `lifecycle.adopt_universe_recommendation(rec_id)` (atomic DB+manifest+audit). New `MockResolver` lets backtests substitute arbitrary predicates without manifest edits.

**Tech Stack:** Node.js (curator, dispatcher, Discord handler); Python 3.13 (MockResolver, lifecycle adoption, doctor, system_checks, backtest CLI flag); PostgreSQL (uses existing migration 112 from Phase A; optional new migration 116 `lifecycle_audit_log` if missing); systemd timer (Saturday 20:00 ET).

**Spec:** `docs/superpowers/specs/2026-05-22-sp2-phase-c-mastermind-universe-recs-design.md`

**Branch:** `feat/sp2-phase-c-mastermind-universe-recs` (off main, after Phase B merges + Soak B clears + `ticker_metadata_history_depth` reports PASS at ≥ 5y)

**Acceptance:**
- `--mode universe-recs` produces deterministic per-strategy grids (same week → identical grid SHA).
- Opus produces valid JSON for ≥ 95% of live strategies in a weekly run.
- Discord reaction flow ✅ adopts atomically; ❌ rejects; ⏸ defers (next-week eligible).
- `adopt_universe_recommendation` survives DB-commit-then-rename-fail and is recoverable.
- First-Saturday operator review supervised; subsequent runs unsupervised.

---

## ⚠️ Codebase conventions

Same Phase A substitutions apply (POSTGRES_URI, psycopg2, node migrate, monkeypatch-style fake clock, `(Status, str)` tuples, `src/system_checks/checks/<name>.py` layout).

**Phase C-specific conventions:**

| In the plan | Use instead | Source |
|---|---|---|
| `claude-bin --model claude-opus-4-7[1m]` | Use `_opus_oneshot.runOneShot({prompt, model})` — already wraps stdin piping, ARG_MAX defense | `src/agent/curators/_opus_oneshot.js` |
| Saturday timer template | Copy `docs/openclaw-position-recs.service` + `.timer`, change ExecStart + OnCalendar | match existing timers |
| Curator file shape | Copy `comprehensive_review.js` (closest existing per-strategy pattern) | `src/agent/curators/comprehensive_review.js` |
| Discord reaction dispatch | Extend the existing `messageReactionAdd` handler in `bot.js` — do NOT create a new dispatcher | `src/channels/discord/bot.js` |
| Manifest path | `src/strategies/manifest.json` | confirmed `find . -name manifest.json -not -path "*/node_modules/*"` |
| `lifecycle_audit_log` table | Check existing migrations first; only add migration 116 if absent | `ls src/database/migrations/ \| grep audit` |
| Opus model id env | `CURATOR_OPUS_MODEL` env var (defaults to `claude-opus-4-7[1m]`) | `_opus_oneshot.js:14` |

---

## ⚠️ Pre-execution corrections (2026-05-24) — READ BEFORE ANY TASK

Grounding against the actual Phase C branch (`8e2b56c`, based on Phase A merge `ef33271`) found the plan's core grid mechanism rests on machinery Phase A explicitly deferred and never built (confirmed unbuilt on Phase B's tip too). These corrections are **authoritative**; where they conflict with Tasks 1–3 as originally written, follow the corrections. Operator decisions captured 2026-05-24.

### C0 — Branch base: wait for Phase B → main
Task 0 Step 2 is **replaced**. Do NOT branch/rebase now. Phase C execution is gated on **PR #9 (Phase B) merging to `main`**. The current branch sits on the Phase A merge (`ef33271`) and lacks ALL Phase B code (5y backfill, quarantine wiring, frozen SP500 history). Once Phase B is on main: `git checkout feat/sp2-phase-c-mastermind-universe-recs && git rebase main`. Land no feature commits on this branch until rebased.

### C1 — The 12-candidate × 8-metric grid must be BUILT first (new Task 0.5)
The plan assumes `regime_blended_backtest --resolver-override --metrics-json` yields 8 metrics. It does **not** exist:
- `regime_blended_backtest.run_with_resolver`, `unified_backtest.run_backtest_with_resolver`, `quick_backtest.run_with_resolver` are all **Phase-A stubs** returning `[{date, universe|signals}]` — no P&L, no metrics. Their docstrings defer the engine join to "Phase B/C".
- `regime_blended_backtest.main()` (line 233) takes **no args** — it ignores argv and only blends precomputed `strategy_regime_backtests` rows. The plan's `--strategy/--start/--end/--resolver-override/--metrics-json` flags would be silently ignored.
- Reachable metrics anywhere = `{sharpe, max_dd, total_return_pct, trade_count}` (4); plan needs 8.

Build the real path in **Task 0.5** (below), hosted in `unified_backtest.py` (it has the only real per-strategy simulation + `aggregate_metrics`). Tasks 1 & 3 then **consume** it.

### C2 — MockResolver: correct shape
Real `UniverseResolver.__init__(db, coverage, manifest_loader=None, today_fn=_date.today, audit_writer=None)` — there is **no** `_load_snapshot`/`coverage_floor`. `resolve()` loads the predicate via `_load_predicate()`, fetches rows via `self._db.fetch_metadata_as_of(as_of)`, floors via `self._coverage.has_floor(symbol, as_of)`, guards look-ahead (`as_of > today` → `AsOfInFutureError`), sorts, caches. The plan's MockResolver (in Task 1 Step 1) is wrong — use this instead:

```python
# src/strategies/universe_resolver.py — append
class MockResolver(UniverseResolver):
    """Phase C grid helper: force a candidate predicate, bypassing the
    manifest-registered one. Reuses resolve()'s db-fetch / coverage-floor /
    look-ahead-guard / sort / cache unchanged."""
    def __init__(self, db, coverage, predicate, **kw):
        super().__init__(db=db, coverage=coverage, **kw)
        self._forced_predicate = predicate
    def _load_predicate(self, strategy_id: str):
        return self._forced_predicate
```

### C3 — Metric set + REGIME-BLENDED definition (operator decision)
`unified_backtest.aggregate_metrics` already emits 5 of 8 (rename at the grid boundary: `hit_rate→win_rate`, `total_trades→trades_n`, `avg_holding_days→mean_holding_days`). Add `sortino` (downside-deviation of the existing `daily_returns`) and `calmar` (annualized `return_pct` ÷ `max_dd_pct`) to `aggregate_metrics`; thread `mean_universe_size` through the run loop. **Grid metrics are regime-blended** (operator chose "match the live sizer" over plain total-period): compute per-regime via the existing `aggregate_per_regime`, then **day-frequency-blend** across the four canonical regimes using the same weighting as `regime_blended_backtest.aggregate_across_regimes`.

### C4 — Cap-predicate caveat (historical market_cap empty)
Until the shares-outstanding backfill lands (Phase B v1 known gap: FMP Starter 403 on historical market-cap), 8 of 12 candidates resolve to **empty universes for historical dates** and would silently select nothing: `r1000, r3000, large_cap, mid_cap, small_cap_liquid, large_cap_options, mid_cap_options, top500_by_adv`. The Task 0.5/T1 grid runs only the **4 cap-independent candidates** (`sp500, options_eligible_only, no_adr, no_otc`) and emits `cap_unverified: true` for the rest. The first supervised Saturday (Task 9) reads cap-gated cells as unverified, not as real recommendations.

---

## Task 0.5: Build resolver→regime-blended 8-metric grid (PREREQUISITE for Tasks 1 & 3)

**Files:**
- Modify: `src/strategies/universe_resolver.py` (MockResolver — C2)
- Modify: `src/backtest/unified_backtest.py` (resolver param + sortino/calmar/mean_universe_size)
- Create: `src/backtest/universe_grid_cli.py` (the CLI Tasks 1/3 shell out to)
- Create: `tests/test_universe_grid.py`

- [ ] **Step 1: MockResolver** — append to `universe_resolver.py` per C2.

- [ ] **Step 2: Resolver injection in `run_backtest`** — add keyword `resolver=None`. When set, inside the `for current_date in oos_dates` loop replace the static `universe` (currently computed once at ~line 412) with `universe = resolver.resolve(strategy_id, as_of=cur_d)` and append `len(universe)` to a `universe_sizes` list. When `resolver is None`, behavior is **byte-identical** (regression-tested). Look-ahead is already guarded inside `resolve()`.

- [ ] **Step 3: Extend `aggregate_metrics`** — add `sortino` (downside-deviation of the existing `daily_returns`; `None` when no downside or <2 points) and `calmar` (annualized `return_pct` ÷ `max_dd_pct`; `None` when `max_dd_pct==0`). Preserve existing keys for back-compat; add new ones alongside. Thread `mean_universe_size = mean(universe_sizes)` into the returned dict.

- [ ] **Step 4: Regime-blend helper** — given the per-regime aggregates from `aggregate_per_regime(trades, regimes)` (trades are already tagged `entry_regime`, verified line 503) and the regime day-frequency over `[start,end]`, produce the blended 8-metric dict. **Blending is per-metric, NOT one rule** (`aggregate_across_regimes` itself day-freq-weights sharpe/return but `max`-es DD and `sum`s trades):

  | Metric | Blend rule across the 4 regimes |
  |---|---|
  | `sharpe`, `sortino`, `calmar` | day-frequency-weighted: `Σ day_freq[r] × m[r]` (ratio-metric convention, matches existing sharpe blend) |
  | `max_dd_pct` | `max` across regimes (worst-case; matches existing) |
  | `trades_n` | `sum` across regimes (matches existing `trade_count`) |
  | `win_rate`, `mean_holding_days` | **trade-count-weighted** across regimes (NOT day-freq — a 2-trade regime must not equal a 200-trade regime) |
  | `mean_universe_size` | day-frequency-weighted (time-average of universe size) |

  Low-sample regimes already NULL their sharpe in `aggregate_per_regime` (`trade_count < 5`); the blender must skip `None` cells and renormalize weights over the contributing regimes.

- [ ] **Step 5: `universe_grid_cli.py`** — argparse `--strategy --start --end --resolver-override <candidate> --metrics-json --seed 42`. Builds `MockResolver(db, coverage, CANDIDATE_PREDICATES[name])` (KeyError on unknown → `sys.exit(2)`), runs unified-with-resolver → `aggregate_per_regime` → blend, prints the 8-key blended JSON to stdout. This is the module Task 1's smoke + Task 3's `_buildGrid` invoke (NOT `regime_blended_backtest`). Note: `unified_backtest` has no random ops today, so `--seed` is accepted for forward-compat/determinism-contract but is currently a no-op — keep it so Task 3's grid command and the determinism test stay stable.

- [ ] **Step 6: Tests (`tests/test_universe_grid.py`)**
  - MockResolver forces predicate (ignores manifest ref).
  - `run_backtest(resolver=None)` unchanged vs a known strategy (regression).
  - CLI emits exactly the 8 keys for `sp500`.
  - 3 cap-independent candidates → 3 distinct metric outputs.
  - Determinism: same `--seed`/window → byte-identical JSON.

- [ ] **Step 7: Commit** — `feat(sp2-c): resolver→regime-blended 8-metric grid (unified_backtest + MockResolver + universe_grid_cli)`

> **Tasks 1 & 3 are amended by this task:** Task 1's MockResolver + CLI flag work now lives here (its Step-1 code block is superseded by C2; its `regime_blended_backtest --resolver-override` smoke becomes `python3 -m src.backtest.universe_grid_cli ...`). Task 3's `_buildGrid` (plan line ~284) must spawn `src.backtest.universe_grid_cli` and iterate **only the 4 cap-independent candidates** (C4) until the cap backfill lands.

---

## Task 0: Branch + workspace setup

**Files:** none (git scaffolding)

- [ ] **Step 1: Confirm Phase B is merged + Soak B passed**

```bash
cd /root/openclaw
git fetch origin
git log origin/main --oneline | grep -i "sp-2 phase b" | head -3
gh pr view <phase-b-pr#> --json state -q .state    # MERGED
python3 -c "
import os, psycopg2; c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute('SELECT min(snapshot_date) FROM ticker_metadata_snapshots')
print('min snapshot_date:', cur.fetchone()[0])
"
# expect ≤ 2021-06-01 (≥ 5y depth)
```

- [ ] **Step 2: Create feature branch**

```bash
git checkout main && git pull
git checkout -b feat/sp2-phase-c-mastermind-universe-recs origin/main
git status   # clean
```

- [ ] **Step 3: Verify Phase A predicates present**

```bash
python3 -c "from src.strategies.universe_default import CANDIDATE_PREDICATES; print(len(CANDIDATE_PREDICATES))"
# expect 12 (or whatever Phase A landed)
```

- [ ] **Step 4: Confirm `lifecycle_audit_log` table existence**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -c "
import os, psycopg2; c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute(\"SELECT 1 FROM information_schema.tables WHERE table_name='lifecycle_audit_log'\")
print('lifecycle_audit_log exists:', bool(cur.fetchone()))
"
```

If FALSE, Task 5 includes migration 116.

---

## Task 1: `MockResolver` + resolver-override CLI flag

**Files:**
- Modify: `src/strategies/universe_resolver.py`
- Modify: `src/backtest/regime_blended_backtest.py`
- Create: `tests/test_resolver_mock.py`

- [ ] **Step 1: Add `MockResolver` class**

```python
# src/strategies/universe_resolver.py — append

class MockResolver(UniverseResolver):
    """Backtest helper: bypasses manifest-registered predicate.

    Used by Phase C universe_recommender to generate per-candidate grids
    without manifest edits.
    """
    def __init__(self, db_conn, prices_parquet, options_parquet, predicate):
        super().__init__(db_conn, prices_parquet, options_parquet)
        self._predicate = predicate

    def resolve(self, strategy_id: str, as_of: date) -> list[str]:
        snapshot = self._load_snapshot(as_of)
        return [m.symbol for m in snapshot
                if self._predicate(m, as_of)
                and self.coverage_floor(m.symbol, as_of)]
```

If `_load_snapshot` is currently private (`__load_snapshot`), rename to single-underscore `_load_snapshot` (subclass access). Update existing callsites in the same file.

- [ ] **Step 2: Backtest CLI flag**

```python
# src/backtest/regime_blended_backtest.py — extend argparse
parser.add_argument('--resolver-override', default=None,
                    help='Phase C: name of a candidate predicate from '
                         'src.strategies.universe_default. If set, uses MockResolver.')
parser.add_argument('--metrics-json', action='store_true',
                    help='Phase C: emit a single JSON object on stdout with the '
                         '8 metrics universe-recs consumes.')
parser.add_argument('--seed', type=int, default=42)
```

In the run() entrypoint:
```python
if args.resolver_override:
    from src.strategies.universe_default import CANDIDATE_PREDICATES
    from src.strategies.universe_resolver import MockResolver
    pred = CANDIDATE_PREDICATES[args.resolver_override]   # KeyError if unknown
    resolver = MockResolver(pg, MASTER_PRICES, MASTER_OPTIONS, pred)
else:
    resolver = UniverseResolver(pg, MASTER_PRICES, MASTER_OPTIONS)
# ... existing run logic ...

if args.metrics_json:
    import json, sys
    json.dump({
        'sharpe': result.sharpe,
        'max_dd_pct': result.max_dd_pct,
        'win_rate': result.win_rate,
        'mean_universe_size': result.mean_universe_size,
        'trades_n': result.trades_n,
        'sortino': result.sortino,
        'calmar': result.calmar,
        'mean_holding_days': result.mean_holding_days,
    }, sys.stdout)
```

- [ ] **Step 3: Tests**

```python
# tests/test_resolver_mock.py
import pytest
from datetime import date
from src.strategies.universe_resolver import UniverseResolver, MockResolver
from src.strategies.universe_default import CANDIDATE_PREDICATES

def test_mock_bypasses_registered_predicate(monkeypatch, tmp_path):
    seen = []
    def fake_predicate(meta, as_of):
        seen.append(meta.symbol); return True
    mr = MockResolver(db_conn=None, prices_parquet=tmp_path, options_parquet=tmp_path, predicate=fake_predicate)
    monkeypatch.setattr(mr, '_load_snapshot', lambda d: _fixture_meta())
    monkeypatch.setattr(mr, 'coverage_floor', lambda *a, **k: True)
    result = mr.resolve('any_id', date(2024, 8, 31))
    assert set(result) == {'AAPL','MSFT'}
    assert set(seen) == {'AAPL','MSFT'}

def _fixture_meta():
    from src.strategies.universe_meta import TickerMetadata
    return [TickerMetadata(symbol='AAPL', ...), TickerMetadata(symbol='MSFT', ...)]
```

Plus a parametrized integration test using `subprocess.run` against `python3 -m src.backtest.regime_blended_backtest --resolver-override <name> --metrics-json` for 3 different candidates → 3 different metric outputs.

- [ ] **Step 4: Run + commit**

```bash
python3 -m pytest tests/test_resolver_mock.py -v
python3 -m src.backtest.regime_blended_backtest --strategy S5_max_pain --start 2025-01-01 --end 2025-06-01 --resolver-override sp500 --metrics-json
git add src/strategies/universe_resolver.py src/backtest/regime_blended_backtest.py tests/test_resolver_mock.py
git commit -m "feat(sp2-c): MockResolver + regime_blended_backtest --resolver-override CLI"
```

---

## Task 2: Opus prompt template

**Files:**
- Create: `src/agent/prompts/subagents/universe-recommender.md`

- [ ] **Step 1: Author the template**

Use the spec §2.2 content verbatim. Use mustache-style `{{var}}` placeholders matching what `universe_recommender.js` will substitute.

- [ ] **Step 2: Commit**

```bash
git add src/agent/prompts/subagents/universe-recommender.md
git commit -m "feat(sp2-c): universe-recommender Opus prompt template"
```

---

## Task 3: `universe_recommender.js`

**Files:**
- Create: `src/agent/curators/universe_recommender.js`

- [ ] **Step 1: Scaffold per spec §2.1 + §2.3**

```js
'use strict';
/**
 * universe_recommender.js — SP-2 Phase C MastermindJohn curator.
 * Saturday 20:00 ET via openclaw-universe-recs.timer.
 */
const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { Pool } = require('pg');
const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const PYTHON       = process.env.PYTHON_BIN || 'python3';
const PROMPT_PATH  = path.join(OPENCLAW_DIR, 'src/agent/prompts/subagents/universe-recommender.md');
const CANDIDATE_SET_VERSION = process.env.UNIVERSE_RECS_CANDIDATE_SET_VERSION || 'v1';
const PER_STRATEGY_BUDGET   = parseFloat(process.env.UNIVERSE_RECS_PER_STRATEGY_BUDGET_USD || '8');
const WEEKLY_BUDGET         = parseFloat(process.env.UNIVERSE_RECS_WEEKLY_BUDGET_USD || '400');
const LOOKBACK_DAYS         = parseInt(process.env.UNIVERSE_RECS_LOOKBACK_DAYS || '365', 10);

const pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });

async function _query(sql, params) { return pool.query(sql, params); }

async function _liveStrategies(strategyIds) {
  const q = strategyIds && strategyIds.length
    ? `WHERE id = ANY($1::text[])`
    : `WHERE status='live' AND deprecated_at IS NULL`;
  const { rows } = await _query(`SELECT id, name, description FROM strategy_registry ${q}`, strategyIds || []);
  return rows;
}

function _readStrategyCode(strategyId) {
  // Manifest gives file path; fall back to convention <id>.py
  const manifest = JSON.parse(fs.readFileSync(path.join(OPENCLAW_DIR, 'src/strategies/manifest.json'), 'utf8'));
  const entry = manifest.strategies[strategyId];
  const filePath = path.join(OPENCLAW_DIR, 'src/strategies/implementations', entry?.file || `${strategyId}.py`);
  return fs.readFileSync(filePath, 'utf8');
}

function _currentPredicate(strategyId) {
  const manifest = JSON.parse(fs.readFileSync(path.join(OPENCLAW_DIR, 'src/strategies/manifest.json'), 'utf8'));
  return manifest.strategies[strategyId]?.metadata?.universe_filter_ref || 'sp500';
}

function _candidateNames() {
  // Python side authoritative; shell out once at startup.
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.universe_default import CANDIDATE_PREDICATES; '
    + 'import json,sys; json.dump(list(CANDIDATE_PREDICATES.keys()), sys.stdout)'],
    {encoding:'utf8'});
  return JSON.parse(r.stdout);
}

async function _buildGrid(strategyId, candidates, weekEnding) {
  const start = new Date(weekEnding); start.setDate(start.getDate() - LOOKBACK_DAYS);
  const startIso = start.toISOString().slice(0,10);
  const endIso   = weekEnding;
  const grid = {};
  for (const c of candidates) {
    const r = spawnSync(PYTHON, [
      '-m','src.backtest.regime_blended_backtest',
      '--strategy', strategyId,
      '--start', startIso, '--end', endIso,
      '--resolver-override', c,
      '--metrics-json',
      '--seed','42',
    ], {encoding:'utf8', timeout: 5*60*1000});
    if (r.status !== 0) {
      grid[c] = null;
      console.warn(`[universe-recs] grid cell ${strategyId}/${c} failed: ${r.stderr}`);
      continue;
    }
    grid[c] = JSON.parse(r.stdout);
  }
  return grid;
}

function _gridSha(grid) {
  return crypto.createHash('sha256').update(JSON.stringify(grid, Object.keys(grid).sort())).digest('hex');
}

function _packPrompt(strategy, code, current, grid) {
  const template = fs.readFileSync(PROMPT_PATH, 'utf8');
  const rows = Object.entries(grid).map(([name, m]) =>
    m ? `| ${name} | ${m.sharpe.toFixed(2)} | ${(m.max_dd_pct*100).toFixed(1)} | ${m.win_rate.toFixed(2)} | ${Math.round(m.mean_universe_size)} | ${m.trades_n} | ${m.sortino.toFixed(2)} | ${m.calmar.toFixed(2)} | ${m.mean_holding_days.toFixed(1)} |`
       : `| ${name} | (grid failed) |  |  |  |  |  |  |  |`
  ).join('\n');
  return template
    .replace(/\{\{strategy_id\}\}/g, strategy.id)
    .replace(/\{\{strategy_name\}\}/g, strategy.name)
    .replace(/\{\{thesis\}\}/g, strategy.description || '(none)')
    .replace(/\{\{loc\}\}/g, code.split('\n').length)
    .replace(/\{\{source_code\}\}/g, code)
    .replace(/\{\{current_predicate\}\}/g, current)
    .replace(/\{\{candidate_names\}\}/g, Object.keys(grid).join(' OR '))
    .replace(/\{\{#each grid\}\}[\s\S]*?\{\{\/each\}\}/, rows);
}

async function _persist(strategy, current, decision, grid, costUsd) {
  const summary = {grid, grid_sha256: _gridSha(grid), candidate_set: CANDIDATE_SET_VERSION};
  const { rows } = await _query(
    `INSERT INTO strategy_universe_recommendations
       (strategy_id, current_predicate, candidate_predicate, candidate_set_id,
        backtest_summary, rationale, approved, mastermind_cost_usd)
     VALUES ($1,$2,$3,$4,$5,$6,NULL,$7) RETURNING id`,
    [strategy.id, current, decision.choice, CANDIDATE_SET_VERSION,
     summary, decision.rationale, costUsd]);
  return rows[0].id;
}

async function _postDiscord(recId, strategy, current, decision, grid) {
  const ch = process.env.DISCORD_UNIVERSE_RECS_CHANNEL || 'universe-recs';
  const cur = grid[current]; const nxt = grid[decision.choice];
  const msg = `**[universe-recs ${new Date().toISOString().slice(0,10)}] ${strategy.name} (\`${strategy.id}\`)**\n` +
    `Current: \`${current}\` (sharpe ${cur?.sharpe?.toFixed(2) ?? 'n/a'}, dd ${(cur?.max_dd_pct*100||0).toFixed(0)}%, trades ${cur?.trades_n||0})\n` +
    `Suggested: \`${decision.choice}\` (sharpe ${nxt?.sharpe?.toFixed(2) ?? 'n/a'}, dd ${(nxt?.max_dd_pct*100||0).toFixed(0)}%, trades ${nxt?.trades_n||0})\n` +
    `Confidence: ${decision.confidence.toFixed(2)}  Expected Δsharpe: ${decision.expected_uplift_sharpe.toFixed(2)}\n` +
    `Rationale: ${decision.rationale}\n` +
    `React: ✅ approve | ❌ reject | ⏸ defer\n` +
    `_footer: universe-rec:${recId}_`;
  // Use existing discord notify helper or webhook
  const { postToChannel } = require('../../channels/discord/notify');   // verify export at impl
  await postToChannel(ch, msg);
}

async function run({dryRun=false, strategyId=null, weekEnding=null}={}) {
  const week = weekEnding || new Date().toISOString().slice(0,10);
  const strategies = await _liveStrategies(strategyId ? [strategyId] : null);
  const candidates = _candidateNames();
  let weeklyCost = 0;
  for (const s of strategies) {
    if (weeklyCost > WEEKLY_BUDGET) { console.warn('[universe-recs] weekly budget hit; stopping'); break; }
    // Skip recently-adopted
    const { rows: r } = await _query(
      `SELECT 1 FROM strategy_universe_recommendations
        WHERE strategy_id=$1 AND adopted=true AND adopted_at > NOW() - INTERVAL '28 days'`,
      [s.id]);
    if (r.length) { console.log(`[universe-recs] skipping ${s.id} (adopted < 28d ago)`); continue; }
    try {
      const code = _readStrategyCode(s.id);
      const cur  = _currentPredicate(s.id);
      const grid = await _buildGrid(s.id, candidates, week);
      if (dryRun) { console.log(`[dry-run] ${s.id} grid_sha=${_gridSha(grid)}`); continue; }
      const prompt = _packPrompt(s, code, cur, grid);
      const res = await runOneShot({prompt, timeoutMs: 30*60*1000});
      const decision = parseJsonBlock(res.text);
      if (!decision || !['no_change', ...candidates].includes(decision.choice)) {
        console.warn(`[universe-recs] ${s.id} bad output, skipping`); continue;
      }
      if (decision.choice === 'no_change') {
        console.log(`[universe-recs] ${s.id} no_change`); weeklyCost += res.costUsd; continue;
      }
      const recId = await _persist(s, cur, decision, grid, res.costUsd);
      await _postDiscord(recId, s, cur, decision, grid);
      weeklyCost += res.costUsd;
      if (res.costUsd > PER_STRATEGY_BUDGET) console.warn(`[universe-recs] ${s.id} OVER per-strategy budget ($${res.costUsd})`);
    } catch (e) {
      console.error(`[universe-recs] ${s.id} failed: ${e.message}`);
    }
  }
  console.log(`[universe-recs] done. weekly cost: $${weeklyCost.toFixed(2)}`);
  await pool.end();
}

module.exports = { run };
```

- [ ] **Step 2: Run a dry-run end-to-end (offline)**

Needs Phase B data to be present; on a Phase C dev box without it, mock the grid by monkeypatching `_buildGrid`:

```bash
node -e "require('./src/agent/curators/universe_recommender').run({dryRun:true, strategyId:'S5_max_pain'})"
```

Expect: `[dry-run] S5_max_pain grid_sha=...`.

- [ ] **Step 3: Commit**

```bash
git add src/agent/curators/universe_recommender.js
git commit -m "feat(sp2-c): universe_recommender curator (grid + Opus + persist + Discord)"
```

---

## Task 4: Wire `--mode universe-recs` into `run_mastermind.js`

**Files:**
- Modify: `src/agent/curators/run_mastermind.js`

- [ ] **Step 1: Add mode dispatch**

In the mode-switch block:
```js
case 'universe-recs': {
  const { run } = require('./universe_recommender');
  await run({
    dryRun: args.includes('--dry-run'),
    strategyId: _extractArg(args, '--strategy-id'),
    weekEnding: _extractArg(args, '--week'),
  });
  break;
}
```

Add the mode to the header doc comment + `--strategy-id` and `--week` flag descriptions.

- [ ] **Step 2: Refuse mode when gate OFF**

```js
if (mode === 'universe-recs' && process.env.OPENCLAW_UNIVERSE_RECS !== '1') {
  console.warn('OPENCLAW_UNIVERSE_RECS != 1; refusing to run. Use --dry-run to bypass.');
  if (!args.includes('--dry-run')) process.exit(0);
}
```

- [ ] **Step 3: Commit**

```bash
node -e "require('./src/agent/curators/run_mastermind.js')"
git add src/agent/curators/run_mastermind.js
git commit -m "feat(sp2-c): --mode universe-recs dispatch + gate"
```

---

## Task 5: `lifecycle.adopt_universe_recommendation` + CLI

**Files:**
- Create: `src/strategies/lifecycle_universe_adoption.py`
- Create: `src/database/migrations/116_lifecycle_audit_log.sql` (only if Task 0 step 4 showed absent)
- Create: `tests/test_universe_adoption.py`

- [ ] **Step 1: If lifecycle_audit_log absent, write migration 116**

```sql
CREATE TABLE IF NOT EXISTS lifecycle_audit_log (
  id            BIGSERIAL PRIMARY KEY,
  event         TEXT NOT NULL,
  strategy_id   TEXT NOT NULL,
  before_state  TEXT,
  after_state   TEXT,
  actor         TEXT NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lcal_strategy ON lifecycle_audit_log(strategy_id, occurred_at DESC);
```

```bash
node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0)).catch(e=>{console.error(e);process.exit(1)})"
```

- [ ] **Step 2: Implement adoption module**

```python
#!/usr/bin/env python3
"""SP-2 Phase C: lifecycle helpers for universe-rec adoption.

Public API:
  adopt_universe_recommendation(rec_id)
  revert_universe_recommendation(strategy_id, all=False)
  list_pending_recommendations()
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
import psycopg2

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'

def _pg(): return psycopg2.connect(os.environ['POSTGRES_URI'])

def adopt_universe_recommendation(rec_id: int) -> None:
    pg = _pg()
    with pg.cursor() as cur:
        cur.execute("""SELECT strategy_id, candidate_predicate
                       FROM strategy_universe_recommendations
                       WHERE id=%s AND approved IS NULL""", (rec_id,))
        row = cur.fetchone()
        if not row: raise ValueError(f"rec {rec_id} missing or already decided")
        strategy_id, candidate = row

        manifest = json.loads(MANIFEST.read_text())
        entry = manifest['strategies'].get(strategy_id)
        if not entry: raise ValueError(f"strategy {strategy_id} not in manifest")
        prior = entry.get('metadata', {}).get('universe_filter_ref')
        entry.setdefault('metadata', {})['universe_filter_ref'] = candidate
        tmp = MANIFEST.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, indent=2))
        os.fsync(open(tmp).fileno())

        cur.execute("""UPDATE strategy_universe_recommendations
                       SET approved=true, approved_at=NOW(),
                           adopted=true, adopted_at=NOW(), approved_by='operator:discord'
                       WHERE id=%s""", (rec_id,))
        cur.execute("""INSERT INTO lifecycle_audit_log
                       (event, strategy_id, before_state, after_state, actor)
                       VALUES ('universe_filter_adopted', %s, %s, %s, 'opus_universe_recs')""",
                    (strategy_id, prior, candidate))
        os.rename(tmp, MANIFEST)
        pg.commit()
    pg.close()

def revert_universe_recommendation(strategy_id: str | None = None, do_all: bool = False) -> None:
    pg = _pg()
    manifest = json.loads(MANIFEST.read_text())
    targets = list(manifest['strategies'].keys()) if do_all else [strategy_id]
    for sid in targets:
        if sid not in manifest['strategies']: continue
        entry = manifest['strategies'][sid]
        prior_ref = entry.get('metadata', {}).get('universe_filter_ref')
        if prior_ref is None: continue
        # Look up prior_state from audit log
        with pg.cursor() as cur:
            cur.execute("""SELECT before_state FROM lifecycle_audit_log
                           WHERE strategy_id=%s AND event='universe_filter_adopted'
                           ORDER BY occurred_at DESC LIMIT 1""", (sid,))
            r = cur.fetchone()
            target_ref = r[0] if r else None
            entry.setdefault('metadata', {})['universe_filter_ref'] = target_ref
            cur.execute("""INSERT INTO lifecycle_audit_log
                           (event, strategy_id, before_state, after_state, actor)
                           VALUES ('universe_filter_reverted', %s, %s, %s, 'operator')""",
                        (sid, prior_ref, target_ref))
        pg.commit()
    tmp = MANIFEST.with_suffix('.tmp'); tmp.write_text(json.dumps(manifest, indent=2))
    os.fsync(open(tmp).fileno()); os.rename(tmp, MANIFEST)
    pg.close()

def list_pending_recommendations() -> list[dict]:
    pg = _pg()
    with pg.cursor() as cur:
        cur.execute("""SELECT id, strategy_id, current_predicate, candidate_predicate,
                               recommended_at, mastermind_cost_usd
                       FROM strategy_universe_recommendations
                       WHERE approved IS NULL ORDER BY recommended_at DESC""")
        rows = cur.fetchall()
    pg.close()
    return [dict(zip(['id','strategy_id','current','candidate','recommended_at','cost'], r)) for r in rows]

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('adopt'); a.add_argument('--rec-id', type=int, required=True)
    r = sub.add_parser('revert'); r.add_argument('--strategy-id'); r.add_argument('--all', action='store_true')
    sub.add_parser('list')
    args = ap.parse_args()
    if args.cmd == 'adopt': adopt_universe_recommendation(args.rec_id)
    elif args.cmd == 'revert':
        if not (args.strategy_id or args.all): ap.error('--strategy-id or --all required')
        revert_universe_recommendation(args.strategy_id, args.all)
    elif args.cmd == 'list':
        for r in list_pending_recommendations(): print(r)

if __name__ == '__main__': main()
```

- [ ] **Step 3: Tests**

```python
# tests/test_universe_adoption.py
import pytest, json, os, uuid, psycopg2
from pathlib import Path
from src.strategies.lifecycle_universe_adoption import (
    adopt_universe_recommendation, revert_universe_recommendation,
    list_pending_recommendations, MANIFEST)

@pytest.fixture
def pg(): c = psycopg2.connect(os.environ['POSTGRES_URI']); yield c; c.close()

def test_adopt_writes_db_and_manifest(pg):
    # Setup: pick an existing strategy_id from manifest, insert a rec
    manifest = json.loads(MANIFEST.read_text())
    sid = next(iter(manifest['strategies'].keys()))
    backup = json.loads(MANIFEST.read_text())
    with pg.cursor() as cur:
        cur.execute("""INSERT INTO strategy_universe_recommendations
            (strategy_id, current_predicate, candidate_predicate, candidate_set_id,
             backtest_summary, rationale, approved, mastermind_cost_usd)
            VALUES (%s, 'sp500', 'large_cap', 'v1', %s::jsonb, 'test', NULL, 0.10)
            RETURNING id""", (sid, json.dumps({'grid':{}})))
        rec_id = cur.fetchone()[0]; pg.commit()
    try:
        adopt_universe_recommendation(rec_id)
        # Verify DB
        with pg.cursor() as cur:
            cur.execute("SELECT adopted, approved FROM strategy_universe_recommendations WHERE id=%s", (rec_id,))
            adopted, approved = cur.fetchone()
            assert adopted and approved
        # Verify manifest
        after = json.loads(MANIFEST.read_text())
        assert after['strategies'][sid]['metadata']['universe_filter_ref'] == 'large_cap'
    finally:
        MANIFEST.write_text(json.dumps(backup, indent=2))
        with pg.cursor() as cur:
            cur.execute("DELETE FROM strategy_universe_recommendations WHERE id=%s", (rec_id,))
            cur.execute("DELETE FROM lifecycle_audit_log WHERE strategy_id=%s AND occurred_at > NOW() - INTERVAL '1 hour'", (sid,))
            pg.commit()

def test_revert_restores_prior(pg):
    # Adopt then revert; manifest should match pre-adopt state.
    ... (mirror of above)
```

- [ ] **Step 4: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_universe_adoption.py -v
git add src/strategies/lifecycle_universe_adoption.py tests/test_universe_adoption.py
[ -f src/database/migrations/116_lifecycle_audit_log.sql ] && git add src/database/migrations/116_lifecycle_audit_log.sql
git commit -m "feat(sp2-c): lifecycle.adopt_universe_recommendation + revert + list + CLI"
```

---

## Task 6: Discord handler + dashboard endpoints + tile

**Files:**
- Modify: `src/channels/discord/bot.js`
- Modify: `src/channels/dashboard/server.js`
- Modify: `src/channels/dashboard/public/index.html`

- [ ] **Step 1: Discord reaction handler**

Locate the existing `messageReactionAdd` handler in `bot.js` and append:

```js
// Phase C: universe-rec reactions
const footer = message.embeds?.[0]?.footer?.text || '';
const isUniRec = footer.startsWith('universe-rec:') || /universe-rec:\d+/.test(message.content || '');
if (isUniRec) {
  const m = (footer + ' ' + (message.content||'')).match(/universe-rec:(\d+)/);
  if (!m) return;
  const recId = m[1];
  const path = reaction.emoji.name === '✅' ? 'approve'
             : reaction.emoji.name === '❌' ? 'reject'
             : reaction.emoji.name === '⏸' ? 'defer'  : null;
  if (!path) return;
  try {
    await fetch(`http://127.0.0.1:7870/api/universe-recs/${recId}/${path}`, {method:'POST'});
    await message.react('✔');
  } catch (e) { console.error('universe-rec reaction handler failed', e); }
}
```

- [ ] **Step 2: Dashboard endpoints**

In `src/channels/dashboard/server.js`:

```js
const { execFileSync } = require('node:child_process');

app.post('/api/universe-recs/:id/:action', async (req, res) => {
  const {id, action} = req.params;
  const fn = {approve:'adopt', reject:'reject', defer:'defer'}[action];
  if (!fn) return res.status(400).json({error:'bad action'});
  try {
    if (fn === 'adopt') {
      execFileSync('python3', ['-m','src.strategies.lifecycle_universe_adoption','adopt','--rec-id',id]);
    } else {
      const pool = req.app.locals.pool;
      const colVal = fn === 'reject' ? {approved:false, adopted:false} : {approved:null};
      await pool.query(`UPDATE strategy_universe_recommendations
                        SET approved=$1, approved_at=NOW(), approved_by='operator:dashboard'
                        WHERE id=$2`, [colVal.approved, id]);
    }
    res.json({ok:true});
  } catch (e) { res.status(500).json({error:String(e)}); }
});

app.get('/api/universe-recs', async (req, res) => {
  const pool = req.app.locals.pool;
  const { rows } = await pool.query(`
    SELECT id, strategy_id, current_predicate, candidate_predicate,
           backtest_summary->>'grid_sha256' AS grid_sha256,
           rationale, approved, adopted, recommended_at, mastermind_cost_usd
    FROM strategy_universe_recommendations
    WHERE recommended_at > NOW() - INTERVAL '14 days'
    ORDER BY recommended_at DESC`);
  res.json(rows);
});
```

- [ ] **Step 3: Operator dashboard tile**

In `src/channels/dashboard/public/index.html`, add a card after the Universe Slice tile:

```html
<div class="card span-12" id="universeRecsCard">
  <h3>Universe Recommendations (last 14 days)</h3>
  <div id="universeRecsTable">Loading…</div>
</div>
<script>
async function refreshUniverseRecs() {
  const rows = await fetch('/api/universe-recs').then(r=>r.json());
  const html = rows.map(r => `
    <tr><td>${r.strategy_id}</td><td>${r.current_predicate||'sp500'}</td>
        <td>${r.candidate_predicate}</td>
        <td>${r.adopted?'✔':r.approved===false?'✗':r.approved===null?'?':'?'}</td>
        <td>${r.rationale.slice(0,80)}…</td>
        <td>
          <button onclick="actUniRec(${r.id},'approve')">✅</button>
          <button onclick="actUniRec(${r.id},'reject')">❌</button>
          <button onclick="actUniRec(${r.id},'defer')">⏸</button>
        </td></tr>`).join('');
  document.getElementById('universeRecsTable').innerHTML =
    `<table><thead><tr><th>Strategy</th><th>Current</th><th>Suggested</th><th>State</th><th>Rationale</th><th>Action</th></tr></thead><tbody>${html}</tbody></table>`;
}
async function actUniRec(id, action) {
  await fetch(`/api/universe-recs/${id}/${action}`, {method:'POST'});
  refreshUniverseRecs();
}
refreshUniverseRecs(); setInterval(refreshUniverseRecs, 60000);
</script>
```

- [ ] **Step 4: Commit**

```bash
node -c src/channels/discord/bot.js
node -c src/channels/dashboard/server.js
git add src/channels/discord/bot.js src/channels/dashboard/server.js src/channels/dashboard/public/index.html
git commit -m "feat(sp2-c): Discord reaction handler + dashboard tile + adopt/reject/defer endpoints"
```

---

## Task 7: Timer + .env + doctor + system_checks

**Files:**
- Create: `docs/openclaw-universe-recs.service`
- Create: `docs/openclaw-universe-recs.timer`
- Modify: `.env.example`
- Modify: `src/maintenance/doctor.py`
- Create: `src/system_checks/checks/universe_recs_health.py`
- Modify: `src/system_checks/checks/__init__.py`
- Create: `tests/test_doctor_universe_recs.py`

- [ ] **Step 1: Systemd units**

```ini
# docs/openclaw-universe-recs.service
[Unit]
Description=OpenClaw Mastermind universe-recs (SP-2 Phase C)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node src/agent/curators/run_mastermind.js --mode universe-recs
StandardOutput=append:/var/log/openclaw-universe-recs.log
StandardError=append:/var/log/openclaw-universe-recs.log
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
```

```ini
# docs/openclaw-universe-recs.timer
[Unit]
Description=Saturday 20:00 ET universe-recs

[Timer]
OnCalendar=Sat *-*-* 20:00:00 America/New_York
Persistent=true
Unit=openclaw-universe-recs.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 2: `.env.example`**

```
# SP-2 Phase C — Mastermind universe-recs
OPENCLAW_UNIVERSE_RECS=0
UNIVERSE_RECS_WEEKLY_BUDGET_USD=400
UNIVERSE_RECS_PER_STRATEGY_BUDGET_USD=8
UNIVERSE_RECS_LOOKBACK_DAYS=365
UNIVERSE_RECS_CANDIDATE_SET_VERSION=v1
DISCORD_UNIVERSE_RECS_CHANNEL=universe-recs
```

- [ ] **Step 3: Doctor check (gated)**

```python
# Append to src/maintenance/doctor.py
@_check('universe_recs_freshness')
def _check_universe_recs_freshness():
    if os.environ.get('OPENCLAW_UNIVERSE_RECS') != '1':
        return Result.PASS, 'gate off; skipped'
    import psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("SELECT max(recommended_at) FROM strategy_universe_recommendations")
        last = cur.fetchone()[0]
    if last is None: return Result.WARN, 'no recommendations yet'
    age_d = (datetime.now(timezone.utc) - last).days
    if age_d > 14: return Result.FAIL, f'last run {age_d}d ago'
    if age_d > 8:  return Result.WARN, f'last run {age_d}d ago'
    return Result.PASS, f'last run {age_d}d ago'
```

- [ ] **Step 4: system_checks**

```python
# src/system_checks/checks/universe_recs_health.py
from src.system_checks.registry import check
from src.system_checks.status import Status

@check(name='universe_recs_health', tags=['agents','strategies'], requires=['db'])
def run() -> tuple[Status, str]:
    import os, psycopg2
    if os.environ.get('OPENCLAW_UNIVERSE_RECS') != '1':
        return Status.PASS, 'gate off; n/a'
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""SELECT count(*) FROM strategy_universe_recommendations
                       WHERE recommended_at > NOW() - INTERVAL '14 days'""")
        recent = cur.fetchone()[0]
    if recent == 0:  return Status.FAIL, 'no recs in 14d'
    if recent < 30:  return Status.WARN, f'only {recent} recs in 14d'
    return Status.PASS, f'{recent} recs in 14d'
```

```python
# Append to src/system_checks/checks/__init__.py
from . import universe_recs_health   # noqa: F401
```

- [ ] **Step 5: Tests**

```python
# tests/test_doctor_universe_recs.py
import subprocess, os
def test_doctor_universe_recs_passes_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_UNIVERSE_RECS', raising=False)
    from src.maintenance.doctor import _check_universe_recs_freshness, Result
    s, _ = _check_universe_recs_freshness(); assert s == Result.PASS

def test_system_check_skips_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_UNIVERSE_RECS', raising=False)
    r = subprocess.run(['python3','-m','src.system_checks','--check','universe_recs_health','--json'],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
```

- [ ] **Step 6: Run + commit**

```bash
python3 -m pytest tests/test_doctor_universe_recs.py -v
git add docs/openclaw-universe-recs.{service,timer} .env.example src/maintenance/doctor.py
git add src/system_checks/checks/universe_recs_health.py src/system_checks/checks/__init__.py tests/test_doctor_universe_recs.py
git commit -m "feat(sp2-c): timer + doctor + system_check (gated on OPENCLAW_UNIVERSE_RECS)"
```

---

## Task 8: Smoke + docs + memory

**Files:**
- Create: `tests/test_universe_recs_smoke.py`
- Modify: `CLAUDE.md`, `ARCHITECTURE.md`
- Create: `/root/.claude/projects/-root/memory/project_sp2_phase_c_universe_recs.md`
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`

- [ ] **Step 1: Smoke test**

```python
# tests/test_universe_recs_smoke.py
import subprocess, os, json

def test_dispatcher_dry_run():
    env = {**os.environ, 'OPENCLAW_UNIVERSE_RECS':'0'}
    r = subprocess.run(['node','src/agent/curators/run_mastermind.js','--mode','universe-recs','--dry-run','--strategy-id','S5_max_pain'],
                       capture_output=True, text=True, timeout=300, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

def test_candidate_set_version_check():
    # CANDIDATE_PREDICATES keys map must match the version env claim
    from src.strategies.universe_default import CANDIDATE_PREDICATES
    assert isinstance(CANDIDATE_PREDICATES, dict) and len(CANDIDATE_PREDICATES) >= 10

def test_prompt_template_renders(tmp_path):
    from pathlib import Path
    p = Path('src/agent/prompts/subagents/universe-recommender.md').read_text()
    for tok in ['{{strategy_id}}','{{thesis}}','{{source_code}}','{{current_predicate}}','{{candidate_names}}']:
        assert tok in p

def test_list_pending_no_crash():
    r = subprocess.run(['python3','-m','src.strategies.lifecycle_universe_adoption','list'],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
```

- [ ] **Step 2: `CLAUDE.md` entry (template, fill after deploy)**

```markdown
- **2026-XX-XX: SP-2 Phase C — Mastermind universe-recs shipped** ...
```

- [ ] **Step 3: `ARCHITECTURE.md`**

Extend the Per-Strategy Universe Resolution section with "Re-evaluation Loop (Mastermind universe-recs)" sub-section: Saturday timer, 12-candidate grid, Opus JSON schema, atomic adoption flow, revert path.

- [ ] **Step 4: Memory**

```markdown
# /root/.claude/projects/-root/memory/project_sp2_phase_c_universe_recs.md
---
name: project-sp2-phase-c-universe-recs
description: "SP-2 Phase C shipped. Mastermind --mode universe-recs every Saturday 20:00 ET; Opus 4.7 1M picks from 12 candidate predicates. Discord ✅/❌/⏸ → lifecycle.adopt_universe_recommendation (atomic DB+manifest+audit). 28-day adopt-skip prevents thrash."
metadata: {node_type: memory, type: project}
---

...
```

Update MEMORY.md index. Replace any stale "Phase C pending" entry.

- [ ] **Step 5: Commit**

```bash
python3 -m pytest tests/test_universe_recs_smoke.py -v
git add tests/test_universe_recs_smoke.py CLAUDE.md ARCHITECTURE.md
git add /root/.claude/projects/-root/memory/project_sp2_phase_c_universe_recs.md
git add /root/.claude/projects/-root/memory/MEMORY.md
git commit -m "docs(sp2-c): smoke tests + CLAUDE/ARCHITECTURE/memory updates"
```

---

## Task 9: PR + first supervised Saturday

- [ ] **Step 1: Full local sweep**

```bash
python3 -m pytest tests/ --ignore=tests/integration_test.py -x --tb=short 2>&1 | tail -50
node test/graph-smoke.js
python3 -m src.system_checks
```

- [ ] **Step 2: Push + open PR**

```bash
git push -u origin feat/sp2-phase-c-mastermind-universe-recs
gh pr create --base main --head feat/sp2-phase-c-mastermind-universe-recs \
  --title "SP-2 Phase C: Mastermind universe-recs (Saturday Opus pick from 12 predicates)" \
  --body "$(cat <<'EOF'
## Summary
- New mode --mode universe-recs on run_mastermind.js; Saturday 20:00 ET timer.
- Per live strategy: deterministic 12-candidate × 8-metric backtest grid (regime_blended_backtest --resolver-override) → Opus 4.7 1M pick → strict-schema JSON → Discord post.
- MockResolver lets backtests substitute predicates without manifest edits.
- Discord ✅/❌/⏸ reactions → dashboard endpoints → lifecycle.adopt_universe_recommendation (atomic DB+manifest+audit-log; two-phase rename pattern).
- 28-day adopt-skip on re-recommendation prevents predicate thrash.
- New: timer unit, doctor check (gated), system_check universe_recs_health, dashboard tile with bypass-Discord approve buttons.
- Migration 116 lifecycle_audit_log (only if not yet present).

Spec: docs/superpowers/specs/2026-05-22-sp2-phase-c-mastermind-universe-recs-design.md
Plan: docs/superpowers/plans/2026-05-22-sp2-phase-c-mastermind-universe-recs.md

## Test plan
- [ ] pytest tests/ green (SP-2 + regression baseline)
- [ ] node test/graph-smoke.js green
- [ ] Soak A (1 week, --dry-run on timer): grids generate cleanly for all 51 strategies
- [ ] Soak B (1 Saturday, gate ON, operator-supervised): all 51 recs reviewed before any approve
- [ ] Soak C (4 weeks unsupervised): weekly $ < $400; ≥ 1 strategy adopted/week; no rollbacks

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Operator deploy + first supervised run**

```bash
ssh vps
cd /root/openclaw && git checkout main && git pull
node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0)).catch(e=>{console.error(e);process.exit(1)})"
sudo cp docs/openclaw-universe-recs.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-universe-recs.timer
systemctl list-timers | grep universe
# First Sat: gate stays OFF (timer dry-runs)
# Second Sat: OPENCLAW_UNIVERSE_RECS=1 ; operator reviews all recs
```

- [ ] **Step 4: Soak monitoring**

Per spec §6.3 + §7.3.

---

## Out of Scope for Phase C

- Free-form predicate emission by Opus.
- Multi-strategy correlated predicate selection.
- Adversarial predicate testing.
- Retroactive backfill of past trades after predicate change.
- PaperHunter / StrategyCoder predicate-at-creation (Phase D).

---

## Spec coverage cross-check

| Spec § | Topic | Task(s) |
|---|---|---|
| 1.2 | Decisions locked | All tasks |
| 2.1 | End-to-end flow | Tasks 3-6 |
| 2.2 | Opus prompt template | Task 2 |
| 2.3 | Determinism + reproducibility | Task 3 (grid SHA + temp=0) |
| 2.4 | MockResolver | Task 1 |
| 2.5 | Discord adoption flow | Task 6 |
| 2.6 | lifecycle.adopt_universe_recommendation | Task 5 |
| 2.7 | Doctor + system_checks | Task 7 |
| 2.8 | Dashboard tile | Task 6 |
| 3.1 | New files | Tasks 1-8 |
| 3.2 | Modified files | Tasks 1, 4, 6, 7 |
| 3.3 | .env changes | Task 7 |
| 3.4 | Schema (migration 116 optional) | Task 5 |
| 3.5 | Memory + docs | Task 8 |
| 4 | Data flow | Tasks 3-7 |
| 6.1 | Failure-mode matrix | Tasks 3, 5, 6 (error paths) |
| 6.2 | Rollback ladder | Tasks 5, 7 (env gates + revert CLI) |
| 6.3 | Pre-deploy checklist | Task 9 |
| 7 | Tests | each task + Task 8 smoke |
