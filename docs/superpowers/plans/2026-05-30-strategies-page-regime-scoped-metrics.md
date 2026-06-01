# Strategies Page — Regime-Scoped Metrics + Regime Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the dashboard Strategies page → Active Stack, default the headline metric columns to an *eligible-regimes blend* (instead of all-regime totals), and restore the dormant per-regime filter (Eligible / All / single regime).

**Architecture:** A new pure JS module `blend_scope.js` ports `universe_grid_cli.blend_metrics`'s rules. `server.js` extends the per-regime SELECT, builds a `metrics_by_scope` object per strategy, and `strategy_row.js` emits it. The client un-hides the existing filter bar, adds an "Eligible" button, and reads the active scope's metrics for the headline columns (projecting them onto the row so existing sort keys keep working). `ALL` scope equals today's exact values → no regression. No schema, no migration, no new endpoint.

**Tech Stack:** Node.js (CommonJS), plain DOM string-templating in `server.js`, `assert`-based node tests (run via `node tests/<file>.js`), PostgreSQL (read-only, existing query extended).

**Spec:** `docs/superpowers/specs/2026-05-30-strategies-page-regime-scoped-metrics-design.md`

---

## Context the implementer needs

- This is the **johnbot-embedded dashboard** at `src/channels/api/server.js` (port 3000 → nginx :80), NOT the `:7870` control room. (Per project memory: user-facing dashboard = `server.js`.)
- `server.js` is one ~10165-line file mixing the `/api/strategies` route (server-side, Node) and the browser client code (template-literal strings emitted to the page). Both live in the same file. Server code runs in Node; client code is inside backtick-delimited HTML/JS strings. **When editing, be sure which side you're on** — server code uses real `require`/`await`; client code is inside a template string and uses `\${...}` escaping.
- **The pure builder** `src/channels/api/strategy_row.js` (`buildStrategyRow`) is `require`-able and unit-tested (`tests/test_api_strategies_backtest.test.js`). New row fields go through it.
- **JS tests** are plain node scripts: `const assert = require('assert'); ...; console.log('ok')`. Run with `cd /root/openclaw && node tests/<file>.js` (exit 0 = pass). No jest/mocha.
- **Per-regime data source:** `strategy_backtest_regimes` (migration `093`) has columns `regime_state, trade_count, sharpe, max_dd_pct, return_pct, hit_rate, avg_pnl_pct, avg_holding_days, oos_days_in_regime`. Rows exist for ALL 4 canonical regimes per strategy (zero-trade regimes written since 2026-05-19), so weight field `oos_days_in_regime` is always present; `avg_pnl_pct`/`avg_holding_days`/`sharpe` are NULL on zero-trade or <5-trade regimes.
- **The blend reference** is `src/backtest/universe_grid_cli.py:blend_metrics` (Python). We port its rules to JS in `blend_scope.js`. We do NOT call Python from the request path.
- **Eligible regimes** at the call site (`server.js:~1355`): `_eligRaw` = registry-derived list, else manifest `eligible_regimes`, else `null` (= eligible-everywhere). `_eligibleSet = new Set(_eligRaw || activeRegimes)`.
- **CANONICAL_REGIMES** order = `['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']`.
- Run client-affecting changes are only verifiable by eye in the browser; this plan verifies the **server + pure-module** behavior with node tests and a live `/api/strategies` smoke, and treats the client edits as mechanical wiring covered by code review.

## File Structure

- **Create:** `src/channels/api/blend_scope.js` — pure `blendScope(breakdown, regimeKeys) -> metrics|null`. One responsibility: blend per-regime rows into one metrics object for a given regime subset.
- **Create:** `tests/test_blend_scope.js` — unit tests for `blendScope`.
- **Modify:** `src/channels/api/server.js` —
  - server: extend `ubtRegimeRows` SELECT + `regime_breakdown` shape (~1211–1241);
  - server: build `metrics_by_scope` + `default_scope` at the row-builder call site (~1358–1369);
  - client: un-hide filter (~8542), re-implement `_stSetRegimeFilter` (~8269), add "Eligible" button (~4089), project active-scope metrics in `_renderActiveStack` (~8559 + cell lines ~8605–8616).
- **Modify:** `src/channels/api/strategy_row.js` — emit `metrics_by_scope` + `default_scope` (pass-through of values computed by the caller).
- **Modify:** `tests/test_api_strategies_backtest.test.js` — assert `metrics_by_scope.ALL` equals legacy top-level values (regression guard).

---

## Task 1: Create the `blendScope` pure module (TDD)

**Files:**
- Create: `src/channels/api/blend_scope.js`
- Create: `tests/test_blend_scope.js`

- [ ] **Step 1: Write the failing test**

Create `tests/test_blend_scope.js`:

```js
// tests/test_blend_scope.js
// Unit tests for blendScope — the per-regime → single-scope metric blender.
const assert = require('assert');
const { blendScope } = require('../src/channels/api/blend_scope');

// Helper: a per-regime breakdown map keyed by regime.
// Fields mirror strategy_backtest_regimes (percent units for max_dd_pct/return_pct).
const BD = {
  LOW_VOL:       { sharpe: 2.0, max_dd_pct: 10, return_pct: 30, hit_rate: 0.60,
                   avg_pnl_pct: 0.012, avg_holding_days: 4, trade_count: 80, oos_days_in_regime: 600 },
  TRANSITIONING: { sharpe: 1.0, max_dd_pct: 5,  return_pct: 8,  hit_rate: 0.50,
                   avg_pnl_pct: 0.004, avg_holding_days: 6, trade_count: 20, oos_days_in_regime: 400 },
  HIGH_VOL:      { sharpe: null, max_dd_pct: 0, return_pct: 0,  hit_rate: null,
                   avg_pnl_pct: null, avg_holding_days: null, trade_count: 0, oos_days_in_regime: 300 },
  CRISIS:        { sharpe: null, max_dd_pct: 0, return_pct: 0,  hit_rate: null,
                   avg_pnl_pct: null, avg_holding_days: null, trade_count: 0, oos_days_in_regime: 100 },
};

// 1. Single-regime blend == identity for that regime
{
  const m = blendScope(BD, ['LOW_VOL']);
  assert.strictEqual(m.closed_count, 80);
  assert.ok(Math.abs(m.sharpe - 2.0) < 1e-9, 'single-regime sharpe = that regime');
  assert.ok(Math.abs(m.win_rate - 0.60) < 1e-9);
  assert.ok(Math.abs(m.arr_pct - 1.2) < 1e-9, 'arr = avg_pnl_pct*100');
  assert.ok(Math.abs(m.act_days - 4) < 1e-9);
  assert.ok(Math.abs(m.adr_pct - 0.3) < 1e-9, 'adr = arr/max(1,act)');
  assert.ok(Math.abs(m.max_dd_pct - 10) < 1e-9);
  assert.ok(Math.abs(m.effective_sharpe - (2.0 / Math.sqrt(4))) < 1e-9, 'eff = sharpe/sqrt(act)');
}

// 2. Two-regime eligible blend (LOW_VOL + TRANSITIONING)
{
  const m = blendScope(BD, ['LOW_VOL', 'TRANSITIONING']);
  // sharpe: day-freq weighted by oos_days_in_regime: (2.0*600 + 1.0*400)/(1000) = 1.6
  assert.ok(Math.abs(m.sharpe - 1.6) < 1e-9, `sharpe blend got ${m.sharpe}`);
  // max_dd = max(10,5) = 10
  assert.ok(Math.abs(m.max_dd_pct - 10) < 1e-9);
  // closed = 80+20 = 100
  assert.strictEqual(m.closed_count, 100);
  // win_rate trade-count weighted: (0.60*80 + 0.50*20)/100 = 0.58
  assert.ok(Math.abs(m.win_rate - 0.58) < 1e-9, `win got ${m.win_rate}`);
  // arr trade-count weighted: (0.012*80 + 0.004*20)/100 *100 = (0.96+0.08)/100*100... compute:
  //   avg_pnl weighted = (0.012*80 + 0.004*20)/100 = (0.96+0.08)/100 = 0.0104 → *100 = 1.04
  assert.ok(Math.abs(m.arr_pct - 1.04) < 1e-9, `arr got ${m.arr_pct}`);
  // act trade-count weighted: (4*80 + 6*20)/100 = (320+120)/100 = 4.4
  assert.ok(Math.abs(m.act_days - 4.4) < 1e-9, `act got ${m.act_days}`);
  // adr = 1.04 / max(1,4.4) = 0.23636...
  assert.ok(Math.abs(m.adr_pct - (1.04 / 4.4)) < 1e-9);
  // eff = 1.6 / sqrt(4.4)
  assert.ok(Math.abs(m.effective_sharpe - (1.6 / Math.sqrt(4.4))) < 1e-9);
}

// 3. Null-sharpe regime is skipped in sharpe blend but counts in closed
{
  const m = blendScope(BD, ['LOW_VOL', 'HIGH_VOL']);
  // HIGH_VOL sharpe null → sharpe blend = LOW_VOL only = 2.0
  assert.ok(Math.abs(m.sharpe - 2.0) < 1e-9, `sharpe skip-null got ${m.sharpe}`);
  // closed still includes HIGH_VOL's 0 trades → 80
  assert.strictEqual(m.closed_count, 80);
}

// 4. Empty regimeKeys → null; keys with no matching rows → null
{
  assert.strictEqual(blendScope(BD, []), null);
  assert.strictEqual(blendScope(BD, ['NONSENSE']), null);
  assert.strictEqual(blendScope(null, ['LOW_VOL']), null);
}

// 5. All-included regimes have zero trades → win/arr/act null, sharpe null, closed 0
{
  const m = blendScope(BD, ['HIGH_VOL', 'CRISIS']);
  assert.strictEqual(m.closed_count, 0);
  assert.strictEqual(m.sharpe, null);
  assert.strictEqual(m.win_rate, null);
  assert.strictEqual(m.arr_pct, null);
  assert.strictEqual(m.act_days, null);
  assert.strictEqual(m.effective_sharpe, null);
}

console.log('ok test_blend_scope');
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && node tests/test_blend_scope.js`
Expected: FAIL — `Error: Cannot find module '../src/channels/api/blend_scope'`.

- [ ] **Step 3: Write the implementation**

Create `src/channels/api/blend_scope.js`:

```js
// src/channels/api/blend_scope.js
// Pure blender: collapse per-regime backtest rows into a single metrics object
// for a chosen subset of regimes. JS port of universe_grid_cli.blend_metrics's
// rules, specialized to the dashboard's headline columns.
//
//   sharpe              : day-frequency-weighted (weight = oos_days_in_regime),
//                         skipping regimes with null sharpe; null if none.
//   max_dd_pct          : max over included regimes (percent units).
//   closed_count        : sum of trade_count over included regimes.
//   win_rate            : trade-count-weighted mean of hit_rate (regimes w/ trades).
//   arr_pct             : trade-count-weighted mean of avg_pnl_pct * 100.
//   act_days            : trade-count-weighted mean of avg_holding_days.
//   adr_pct             : arr_pct / max(1, act_days).
//   effective_sharpe    : sharpe / sqrt(act_days)  (matches backtest_panel).
//   return_pct          : day-freq-weighted mean of return_pct (informational).
//
// breakdown: { REGIME: { sharpe, max_dd_pct, return_pct, hit_rate, avg_pnl_pct,
//                        avg_holding_days, trade_count, oos_days_in_regime }, ... }
// regimeKeys: array of regimes to include.
// Returns a metrics object, or null when no included regime has a row.
function blendScope(breakdown, regimeKeys) {
  if (!breakdown || !Array.isArray(regimeKeys) || regimeKeys.length === 0) return null;
  const rows = regimeKeys.map(rg => breakdown[rg]).filter(Boolean);
  if (rows.length === 0) return null;

  // day-frequency-weighted mean over rows whose `key` is non-null
  const dayFreqWeighted = (key) => {
    const contrib = rows.filter(r => r[key] != null);
    if (!contrib.length) return null;
    let wsum = 0, vsum = 0;
    for (const r of contrib) {
      const w = Number(r.oos_days_in_regime) || 0;
      wsum += w; vsum += Number(r[key]) * w;
    }
    if (wsum < 1e-12) {
      // all weights zero → equal weight
      return contrib.reduce((a, r) => a + Number(r[key]), 0) / contrib.length;
    }
    return vsum / wsum;
  };

  // trade-count-weighted mean over rows whose `key` is non-null AND trade_count>0
  const tradeWeighted = (key) => {
    const contrib = rows.filter(r => r[key] != null && (Number(r.trade_count) || 0) > 0);
    if (!contrib.length) return null;
    let tc = 0, vsum = 0;
    for (const r of contrib) {
      const c = Number(r.trade_count) || 0;
      tc += c; vsum += Number(r[key]) * c;
    }
    return tc > 0 ? vsum / tc : null;
  };

  const closed_count = rows.reduce((a, r) => a + (Number(r.trade_count) || 0), 0);
  const max_dd_pct = rows.reduce((a, r) => Math.max(a, Number(r.max_dd_pct) || 0), 0);
  const sharpe = dayFreqWeighted('sharpe');
  const return_pct = dayFreqWeighted('return_pct');
  const win_rate = closed_count > 0 ? tradeWeighted('hit_rate') : null;
  const act_days = closed_count > 0 ? tradeWeighted('avg_holding_days') : null;
  const avg_pnl = closed_count > 0 ? tradeWeighted('avg_pnl_pct') : null;
  const arr_pct = avg_pnl != null ? avg_pnl * 100 : null;
  const adr_pct = (arr_pct != null && act_days) ? (arr_pct / Math.max(1, act_days)) : null;
  const effective_sharpe = (sharpe != null && act_days && act_days > 0)
    ? (sharpe / Math.sqrt(act_days)) : null;

  return {
    sharpe, effective_sharpe, return_pct, max_dd_pct,
    closed_count, win_rate, arr_pct, adr_pct, act_days,
  };
}

module.exports = { blendScope };
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /root/openclaw && node tests/test_blend_scope.js`
Expected: `ok test_blend_scope` (exit 0).

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/channels/api/blend_scope.js tests/test_blend_scope.js
git commit -m "feat(dashboard): add blendScope per-regime metric blender + tests

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Extend the per-regime SELECT + breakdown shape in server.js

**Files:**
- Modify: `src/channels/api/server.js` (the `ubtRegimeRows` query ~1211–1220 and the `regime_breakdown` assembly ~1232–1242)

- [ ] **Step 1: Extend the SQL SELECT**

In `src/channels/api/server.js`, find the `ubtRegimeRows` query (currently selecting `br.strategy_id... br.regime_state, br.trade_count, br.sharpe, br.max_dd_pct, br.return_pct, br.hit_rate`). Replace its column list to also pull the three blend fields. The block currently reads:

```js
    const ubtRegimeRows = (await dbQuery(`
      SELECT r.strategy_id, br.regime_state, br.trade_count, br.sharpe,
             br.max_dd_pct, br.return_pct, br.hit_rate
      FROM strategy_backtest_regimes br
```

Change the SELECT line to:

```js
    const ubtRegimeRows = (await dbQuery(`
      SELECT r.strategy_id, br.regime_state, br.trade_count, br.sharpe,
             br.max_dd_pct, br.return_pct, br.hit_rate,
             br.avg_pnl_pct, br.avg_holding_days, br.oos_days_in_regime
      FROM strategy_backtest_regimes br
```

- [ ] **Step 2: Carry the new fields into `regime_breakdown`**

Find the loop that builds `entry.regime_breakdown[r.regime_state]` (~1235). It currently sets `sharpe, max_dd, total_return_pct, trade_count, hit_rate`. Add the three raw fields AND a raw `max_dd_pct` (percent) for the blender — keep the existing fraction `max_dd` for the chip-grid tooltip. Replace the assignment block:

```js
      entry.regime_breakdown[r.regime_state] = {
        sharpe:      r.sharpe,
        max_dd:      r.max_dd_pct != null ? r.max_dd_pct / 100 : null,  // breakdown JSON uses fraction
        total_return_pct: r.return_pct,
        trade_count: r.trade_count,
        hit_rate:    r.hit_rate,
      };
```

with:

```js
      entry.regime_breakdown[r.regime_state] = {
        sharpe:      r.sharpe,
        max_dd:      r.max_dd_pct != null ? r.max_dd_pct / 100 : null,  // breakdown JSON uses fraction
        total_return_pct: r.return_pct,
        trade_count: r.trade_count,
        hit_rate:    r.hit_rate,
        // Raw per-regime fields consumed by blendScope (percent units kept as-is):
        max_dd_pct:        r.max_dd_pct,
        return_pct:        r.return_pct,
        avg_pnl_pct:       r.avg_pnl_pct,
        avg_holding_days:  r.avg_holding_days,
        oos_days_in_regime: r.oos_days_in_regime,
      };
```

- [ ] **Step 3: Verify the route still loads (syntax + smoke)**

Run: `cd /root/openclaw && node -e "require('./src/channels/api/server.js')" 2>&1 | head -5 || true`
Expected: No SyntaxError. (The module may try to bind a port; a port-in-use or similar runtime message is fine — we only care that it PARSES. If it errors with `SyntaxError`, fix the edit. If it hangs binding a port, Ctrl-C is fine — parse already succeeded.)

Alternative pure parse check (preferred, no side effects):
Run: `cd /root/openclaw && node --check src/channels/api/server.js && echo PARSE_OK`
Expected: `PARSE_OK`.

- [ ] **Step 4: Commit**

```bash
cd /root/openclaw
git add src/channels/api/server.js
git commit -m "feat(dashboard): surface per-regime avg_pnl/holding/oos for metric blending

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Build `metrics_by_scope` + `default_scope` and emit through strategy_row (TDD)

**Files:**
- Modify: `src/channels/api/strategy_row.js`
- Modify: `src/channels/api/server.js` (row-builder call site ~1358–1369)
- Modify: `tests/test_api_strategies_backtest.test.js` (regression assertion)

- [ ] **Step 1: Add the regression assertion to the existing row test**

In `tests/test_api_strategies_backtest.test.js`, the existing test calls `buildStrategyRow({...})`. Extend that call to pass the two new inputs, and assert ALL-scope equals legacy values. Add these inputs to the existing `buildStrategyRow({...})` argument object (insert before the closing `})`):

```js
  metricsByScope: {
    ALL:      { sharpe: 2.0, effective_sharpe: 1.0, return_pct: 50, max_dd_pct: 12,
                closed_count: 120, win_rate: 0.55, arr_pct: 1.2, adr_pct: 0.3, act_days: 4 },
    ELIGIBLE: { sharpe: 1.8, effective_sharpe: 0.9, return_pct: 30, max_dd_pct: 10,
                closed_count: 80, win_rate: 0.60, arr_pct: 1.2, adr_pct: 0.3, act_days: 4 },
  },
  defaultScope: 'ELIGIBLE',
```

Then add these assertions before the final `console.log('ok')`:

```js
assert.ok(row.metrics_by_scope, 'metrics_by_scope emitted');
assert.strictEqual(row.default_scope, 'ELIGIBLE');
// ALL scope must equal the legacy top-level values (no regression).
assert.strictEqual(row.metrics_by_scope.ALL.sharpe, row.sharpe);
assert.strictEqual(row.metrics_by_scope.ALL.closed_count, row.closed_count);
assert.strictEqual(row.metrics_by_scope.ALL.win_rate, row.win_rate);
assert.strictEqual(row.metrics_by_scope.ALL.arr_pct, row.arr_pct);
```

- [ ] **Step 2: Run the row test to verify it fails**

Run: `cd /root/openclaw && node tests/test_api_strategies_backtest.test.js`
Expected: FAIL — `row.metrics_by_scope` is undefined (assert throws).

- [ ] **Step 3: Emit the new fields from `buildStrategyRow`**

In `src/channels/api/strategy_row.js`, inside the returned object (after `has_backtest_panel: !!x.panel,` and before the `// ── Live` comment), add:

```js
    // Per-regime-scoped metric variants. ALL == the legacy top-level fields
    // (built by the caller from the same run/panel/bestWorst sources); ELIGIBLE
    // + single-regime scopes are blendScope() outputs. default_scope tells the
    // client which to show first. Absent → client falls back to top-level r.*.
    metrics_by_scope: x.metricsByScope || null,
    default_scope:    x.defaultScope || 'ALL',
```

- [ ] **Step 4: Run the row test to verify it passes**

Run: `cd /root/openclaw && node tests/test_api_strategies_backtest.test.js`
Expected: `ok` (exit 0).

- [ ] **Step 5: Build `metrics_by_scope` at the server call site**

In `src/channels/api/server.js`, immediately BEFORE the `rows.push({ ...buildStrategyRow({` call at ~1361 (the live/manifest branch), insert the scope-building block. It reuses the same `run`/`panel`/`bestWorst` math `strategy_row.js` uses for ALL, then blends ELIGIBLE + each single regime. Add this just after the `_decoratedBreakdown` line (~1360):

```js
      // ── Regime-scoped metric variants (Active Stack filter) ──────────────
      // ALL mirrors the legacy top-level fields exactly (built from the same
      // run/panel/bestWorst sources). ELIGIBLE + single-regime come from
      // blendScope over the raw per-regime breakdown.
      const _runForScope   = ubtRunById[sid] || {};
      const _panelForScope = panelById[sid] || null;
      const _bwForScope    = bwById[sid] || {};
      const _actAll = _runForScope.avg_holding_days != null ? Number(_runForScope.avg_holding_days) : null;
      const _arrAll = _bwForScope.avg_pnl != null ? Number(_bwForScope.avg_pnl) * 100 : null;
      const _allScope = {
        sharpe:           _runForScope.total_sharpe ?? null,
        effective_sharpe: _panelForScope?.effective_sharpe ?? null,
        return_pct:       _runForScope.total_return_pct ?? null,
        max_dd_pct:       _runForScope.total_max_dd_pct ?? null,
        closed_count:     _runForScope.total_trades ?? 0,
        win_rate:         _runForScope.total_hit_rate ?? null,
        arr_pct:          _arrAll,
        adr_pct:          (_arrAll != null && _actAll) ? (_arrAll / Math.max(1, _actAll)) : null,
        act_days:         _actAll,
      };
      const _rawBd = unifiedBacktest[sid]?.regime_breakdown ?? null;
      const _metricsByScope = { ALL: _allScope };
      for (const _rg of _CANON_AXIS) {
        const _s = blendScope(_rawBd, [_rg]);
        if (_s) _metricsByScope[_rg] = _s;
      }
      // ELIGIBLE: blend over the eligible set; null (eligible-everywhere) ⇒ ALL.
      const _eligScope = _eligRaw ? blendScope(_rawBd, _eligRaw) : null;
      _metricsByScope.ELIGIBLE = _eligScope || _allScope;
      const _defaultScope = _eligRaw ? 'ELIGIBLE' : 'ALL';
```

Then add the two fields to the `buildStrategyRow({...})` argument object (in the same call, alongside `regimeBreakdown: _decoratedBreakdown,`):

```js
          metricsByScope: _metricsByScope,
          defaultScope: _defaultScope,
```

- [ ] **Step 6: Require `blendScope` at the top of server.js**

Find the line `const { buildStrategyRow } = require('./strategy_row');` (~line 12). Add immediately after it:

```js
const { blendScope } = require('./blend_scope');
```

- [ ] **Step 7: Verify parse + the row test still passes**

Run: `cd /root/openclaw && node --check src/channels/api/server.js && node tests/test_api_strategies_backtest.test.js`
Expected: `PARSE_OK` not required here (no echo), but no SyntaxError, then `ok`. If `node --check` prints nothing it succeeded; confirm the row test prints `ok`.

- [ ] **Step 8: Commit**

```bash
cd /root/openclaw
git add src/channels/api/server.js src/channels/api/strategy_row.js tests/test_api_strategies_backtest.test.js
git commit -m "feat(dashboard): build per-scope metrics on /api/strategies rows

ALL scope == legacy values; ELIGIBLE + single-regime via blendScope.
default_scope=ELIGIBLE unless eligible-everywhere (then ALL).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Live smoke — confirm scopes differ on a real subset-eligible strategy

**Files:** none (verification only)

- [ ] **Step 1: Query a real subset-eligible live strategy via the builder path**

This runs the actual server query + blender against the live DB, read-only, no writes. Run:

```bash
cd /root/openclaw
export POSTGRES_URI=$(grep '^POSTGRES_URI=' .env | cut -d= -f2-)
node -e '
const { Client } = require("pg");
const { blendScope } = require("./src/channels/api/blend_scope");
(async () => {
  const c = new Client({ connectionString: process.env.POSTGRES_URI });
  await c.connect();
  const { rows } = await c.query(`
    SELECT r.strategy_id, br.regime_state, br.trade_count, br.sharpe, br.max_dd_pct,
           br.return_pct, br.hit_rate, br.avg_pnl_pct, br.avg_holding_days, br.oos_days_in_regime
    FROM strategy_backtest_regimes br
    JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_id
            FROM strategy_backtest_runs WHERE primary_window=TRUE
            ORDER BY strategy_id, run_at DESC) q ON q.run_id = br.run_id`);
  const byStrat = {};
  for (const r of rows) (byStrat[r.strategy_id] ??= {})[r.regime_state] = r;
  // pick a strategy that has trades in >=2 regimes so a subset blend differs from ALL
  let picked = null;
  for (const [sid, bd] of Object.entries(byStrat)) {
    const withTrades = Object.values(bd).filter(x => (x.trade_count||0) > 0);
    if (withTrades.length >= 2) { picked = sid; break; }
  }
  const bd = byStrat[picked];
  const all = blendScope(bd, ["LOW_VOL","TRANSITIONING","HIGH_VOL","CRISIS"]);
  const one = blendScope(bd, [Object.keys(bd).find(k => (bd[k].trade_count||0) > 0)]);
  console.log("strategy:", picked);
  console.log("ALL-blend   sharpe=", all && all.sharpe, "closed=", all && all.closed_count);
  console.log("single-rg   sharpe=", one && one.sharpe, "closed=", one && one.closed_count);
  console.log("DIFFER:", JSON.stringify(all) !== JSON.stringify(one));
  await c.end();
})();
'
```
Expected: prints a strategy id, an ALL-blend and a single-regime blend with **different** sharpe/closed, and `DIFFER: true`. (Confirms blendScope works on real data and a regime subset genuinely changes the numbers.)

- [ ] **Step 2: No commit** (verification only). If `DIFFER` is false for every strategy (extremely unlikely), report it — it would mean all live strategies trade in exactly one regime, in which case eligible-blend == single-regime by construction (still correct).

---

## Task 5: Client — un-hide filter, add Eligible button, re-implement setter

**Files:**
- Modify: `src/channels/api/server.js` (filter-bar markup ~4089, `_stRegimeFilter` init ~8267, `_stSetRegimeFilter` ~8269, `_renderActiveStack` un-hide ~8542)

- [ ] **Step 1: Add the "Eligible" button to the filter bar**

In the `<div class="st-regime-filter" id="st-regime-filter">` block (~4087), the first button is `data-regime="ALL" ... class="srf-btn active"`. Change it so **Eligible** is the default-active button and All is no longer default-active. Replace:

```js
        <button class="srf-btn active" data-regime="ALL"           onclick="_stSetRegimeFilter('ALL')">All</button>
```

with:

```js
        <button class="srf-btn active" data-regime="ELIGIBLE"      onclick="_stSetRegimeFilter('ELIGIBLE')">Eligible</button>
        <button class="srf-btn" data-regime="ALL"                  onclick="_stSetRegimeFilter('ALL')">All</button>
```

- [ ] **Step 2: Change the filter state default**

Find `let _stRegimeFilter = 'ALL';` (~8267). Replace with:

```js
let _stRegimeFilter = 'ELIGIBLE'; // 'ELIGIBLE' | 'ALL' | 'LOW_VOL' | 'TRANSITIONING' | 'HIGH_VOL' | 'CRISIS'
```

- [ ] **Step 3: Re-implement `_stSetRegimeFilter`**

Replace the entire no-op `_stSetRegimeFilter` function (~8269–8275) with:

```js
function _stSetRegimeFilter(rg) {
  _stRegimeFilter = rg;
  // Toggle the active button class.
  const bar = document.getElementById('st-regime-filter');
  if (bar) {
    bar.querySelectorAll('.srf-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.regime === rg);
    });
    const hint = document.getElementById('srf-hint');
    if (hint) {
      hint.textContent = rg === 'ALL'
        ? 'showing all-regime backtest stats'
        : rg === 'ELIGIBLE'
          ? 'showing eligible-regimes blend (regimes the strategy is approved to trade)'
          : 'showing ' + rg + ' backtest stats';
    }
  }
  _renderActiveStack(strategiesData.filter(_inActiveStack));
}
```

- [ ] **Step 4: Un-hide the filter bar in `_renderActiveStack`**

Find the block in `_renderActiveStack` (~8538–8543):

```js
  // The per-regime filter tab once swapped the metric columns to a LIVE
  // per-regime breakdown. Those columns are now backtest-sourced and that
  // live breakdown is gone from the payload, so the filter has no coherent
  // target — hide the orphaned control.
  const _rf = document.getElementById('st-regime-filter');
  if (_rf) _rf.style.display = 'none';
```

Replace it with:

```js
  // Regime filter: scopes the headline metric columns to ALL / ELIGIBLE / a
  // single regime via each row's metrics_by_scope. Show the control.
  const _rf = document.getElementById('st-regime-filter');
  if (_rf) _rf.style.display = '';
```

- [ ] **Step 5: Verify parse**

Run: `cd /root/openclaw && node --check src/channels/api/server.js && echo PARSE_OK`
Expected: `PARSE_OK`.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw
git add src/channels/api/server.js
git commit -m "feat(dashboard): restore regime filter bar with Eligible default

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Client — project active-scope metrics onto rows + read in cells

**Files:**
- Modify: `src/channels/api/server.js` (`_renderActiveStack` enrich ~8559, cell reads ~8605–8616)

- [ ] **Step 1: Project the active scope's metrics onto each enriched row**

In `_renderActiveStack`, find the `enriched` mapping (~8559):

```js
  const enriched = rows.map(r => Object.assign({}, r, {
    _oue_total:   (r.oue_over || 0) + (r.oue_under || 0) + (r.oue_expected || 0),
    _active_rank: _activeRankFor(r),
  }));
```

Replace with a version that overlays the active scope's metric fields (so the existing sort keys `sharpe`, `effective_sharpe`, `closed_count`, `win_rate`, `arr_pct`, `adr_pct`, `act_days` keep working unchanged). Note `max_dd` is not a headline column in this table, so we overlay only the seven shown metrics + their backing fields:

```js
  // Resolve which metric scope to display (regime filter). Fall back to the
  // row's own default_scope, then to the legacy top-level fields.
  const _scope = _stRegimeFilter;
  const _pickScope = (r) => {
    const mbs = r.metrics_by_scope;
    if (!mbs) return null;
    return mbs[_scope] || mbs[r.default_scope] || mbs.ALL || null;
  };
  const enriched = rows.map(r => {
    const m = _pickScope(r);
    const overlay = m ? {
      sharpe:           m.sharpe,
      effective_sharpe: m.effective_sharpe,
      closed_count:     m.closed_count,
      win_rate:         m.win_rate,
      arr_pct:          m.arr_pct,
      adr_pct:          m.adr_pct,
      act_days:         m.act_days,
    } : {};
    return Object.assign({}, r, overlay, {
      _oue_total:   (r.oue_over || 0) + (r.oue_under || 0) + (r.oue_expected || 0),
      _active_rank: _activeRankFor(r),
    });
  });
```

This means the existing cell code (`const sh = r.sharpe`, `const arr = r.arr_pct`, etc. inside `shown.map(r => ...)`) automatically reads the scoped values, because `shown` derives from `enriched`. **No change needed to the cell-rendering lines** (~8605–8616) — they read `r.sharpe`/`r.arr_pct`/`r.closed_count`/etc., which are now the overlaid scope values.

- [ ] **Step 2: Add a scope tag to the metric column headers when scoped**

In the header row of the active table (~8587–8595, the `<th>` cells for Sharpe / Eff.Sharpe / Closed / Win% / ARR% / ADR% / ACT), add a small scope indicator so the operator knows the columns are filtered. Immediately before the `<tr>` of column headers (the `el.innerHTML = \`<table ...><tr>` block at ~8583), compute a suffix; then append it to the relevant header titles. Simplest non-invasive approach: insert a scope caption into the existing `By Regime` header is wrong (that's the chip grid). Instead, add a one-line caption above the table. Find:

```js
  el.innerHTML = \`<table class="db-table st-active-table" style="min-width:1180px">
    <tr>
```

Replace with:

```js
  const _scopeLabel = _scope === 'ALL' ? 'All regimes'
    : _scope === 'ELIGIBLE' ? 'Eligible regimes'
    : _scope + ' only';
  el.innerHTML = \`<div class="st-scope-caption" style="font-size:9.5px;color:var(--dim);padding:2px 4px 6px;letter-spacing:.04em">Metrics scope: <b style="color:var(--muted)">\${_scopeLabel}</b> — Sharpe / Eff / Closed / Win / ARR / ADR / ACT reflect this regime selection. "By Regime" always shows all four.</div>
  <table class="db-table st-active-table" style="min-width:1180px">
    <tr>
```

- [ ] **Step 3: Verify parse**

Run: `cd /root/openclaw && node --check src/channels/api/server.js && echo PARSE_OK`
Expected: `PARSE_OK`.

- [ ] **Step 4: Commit**

```bash
cd /root/openclaw
git add src/channels/api/server.js
git commit -m "feat(dashboard): scope Active Stack headline metrics to selected regime

Overlay metrics_by_scope[scope] onto rows so cells + sort read scoped
values; caption shows the active scope.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full regression + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the JS tests touched by this change**

Run:
```bash
cd /root/openclaw
node tests/test_blend_scope.js && \
node tests/test_api_strategies_backtest.test.js && \
echo ALL_JS_TESTS_OK
```
Expected: `ok test_blend_scope`, then `ok`, then `ALL_JS_TESTS_OK`.

- [ ] **Step 2: Parse-check the whole server module**

Run: `cd /root/openclaw && node --check src/channels/api/server.js && node --check src/channels/api/strategy_row.js && node --check src/channels/api/blend_scope.js && echo PARSE_OK`
Expected: `PARSE_OK`.

- [ ] **Step 3: Confirm no other consumer of the changed fields broke**

The change is purely additive to row payload + an overlay in one renderer. Confirm the candidate table + expansion panel still reference legacy fields (they read `r.backtest_sharpe` / `r.backtest_regime_breakdown`, untouched). Run a grep to confirm we didn't rename anything they depend on:

```bash
cd /root/openclaw
grep -n "backtest_regime_breakdown\|backtest_sharpe\|r.sharpe\b" src/channels/api/server.js | head
```
Expected: those references still exist and are unchanged (the candidate renderer `_regimeBacktestSharpe` and `_renderCandidates` still use them).

- [ ] **Step 4: No commit** (verification only).

---

## Self-Review

**1. Spec coverage:**
- Default headline = eligible-regimes blend → Task 3 (`_defaultScope='ELIGIBLE'`, `metrics_by_scope.ELIGIBLE`) + Task 6 (overlay reads default_scope). ✓
- Restore per-regime filter → Task 5 (un-hide, Eligible button, working setter) + Task 6 (scoped cells). ✓
- Blend logic mirrors `blend_metrics` → Task 1 (`blendScope`, day-freq sharpe / max-dd max / trade-count win+arr+act). ✓
- Per-scope effective_sharpe derived `sharpe/√act` → Task 1. ✓
- ALL == legacy values (no regression) → Task 3 Step 1 assertions + `_allScope` built from same sources. ✓
- eligible-everywhere → default ALL → Task 3 (`_eligRaw ? 'ELIGIBLE' : 'ALL'`). ✓
- max_dd units (blend on percent) → Task 1 (uses `max_dd_pct` percent) + Task 2 (carries raw `max_dd_pct`). ✓
- Graceful degradation (no breakdown / stale tab) → Task 1 returns null; Task 6 `_pickScope` falls back to default_scope→ALL→legacy `r.*`. ✓
- Single regime = raw row → Task 1 test #1 + Task 4 smoke. ✓
- Sort keeps working → Task 6 Step 1 overlays metric fields onto rows before sort. ✓
- Scope = Active Stack only → Tasks 5–6 touch only `_renderActiveStack`/its filter; candidate table untouched (Task 7 Step 3 confirms). ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". Every code step shows full code. ✓

**3. Type consistency:** `blendScope(breakdown, regimeKeys)` defined Task 1, called identically Tasks 3–4. `metrics_by_scope`/`default_scope` (row fields, snake_case) vs `metricsByScope`/`defaultScope` (builder input, camelCase) — consistent with the existing `strategy_row.js` convention (`eligRaw`→`eligible_regimes`). `_pickScope`/`_scope`/`_stRegimeFilter` consistent across Tasks 5–6. Metric object keys (`sharpe, effective_sharpe, return_pct, max_dd_pct, closed_count, win_rate, arr_pct, adr_pct, act_days`) identical in Task 1 output, Task 3 `_allScope`, Task 3 test, Task 6 overlay. ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-30-strategies-page-regime-scoped-metrics.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review (spec compliance then code quality) between tasks. (Per the 2-core VPS memory: dispatch sequentially, not in parallel — these tasks share `server.js` so they're inherently sequential anyway.)

**2. Inline Execution** — execute tasks in this session via executing-plans, batch with checkpoints.

**Which approach?**
