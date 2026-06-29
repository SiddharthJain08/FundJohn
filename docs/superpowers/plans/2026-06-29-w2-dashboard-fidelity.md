# W2 Dashboard-Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the user dashboard (`src/channels/api/server.js` :3000) reflect reality exactly and stop presenting dead/stale controls, without changing the live trading path beyond the gated D1/D2/D8 items.

**Architecture:** New pure logic lands in small testable helper modules in `src/channels/api/` (mirroring `regime_active.js`, `positions_grouped.js`); routes/inline-render in `server.js` consume them. Tests are plain Node `assert` scripts run with `node tests/test_*.js` (existing convention — see `tests/test_regime_active.js`). Phase 1 (C1–C5, low-risk) lands then STOPS for operator review; Phase 2 (C6–C9, live-touching) follows after sign-off.

**Tech Stack:** Node 22 (`node:test` available but repo convention is `require('assert')` scripts), Express, PostgreSQL (`src/database/postgres`), Alpaca CLI via `runAlpaca`.

## Global Constraints

- **Path-scoped commits only.** Never `git add -A`/`.`. The live tree carries uncommitted WIP (`src/strategies/manifest.json`, `src/strategies/registry.py`, untracked `src/strategies/implementations/S_*` — the mastermind is actively coding). Stage each file explicitly; abort if anything unexpected is staged. Never `git reset --hard` / `git clean` / blind `git checkout`.
- **No master-data deletion.** Only `pipeline_config` (config table) is de-duplicated (C9). Never touch `prices/options_eod/financials/macro/insider/earnings/prices_30m/historical_regimes/crypto_bars_1h.parquet` or the canonical Postgres tables.
- **No live-book mutation without operator sign-off** (the D1c sign-off sheet gate).
- **VPS 2-core/8GB no-swap:** these tasks are light, but never load full parquets; keep DB queries bounded.
- **Commit message footer:** end every commit with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Restart to apply server.js changes is a discrete operator-acknowledged step at the very end** — the user-scope `johnbot.service` ONLY (never the disabled system unit → EADDRINUSE on :3000).

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/channels/api/leverage.js` (new) | pure: realized broker leverage from account values | C1 |
| `src/channels/api/regime_freshness.js` (new) | pure: detect stale daily regime block | C2 |
| `src/channels/api/pipelines_summary.js` (new) | pure: tile counts from live+durable runs | C3 |
| `src/channels/api/regime_active.js` (modify) | add `regimeForStrategy()` crypto/equity selector | C4 |
| `src/channels/api/server.js` (modify) | consume helpers in routes/inline render; relabels | C1,C2,C4,C5 |
| `src/channels/api/routes_pipelines.js` (modify) | `/summary` uses `pipelines_summary.js` | C3 |
| `src/channels/api/routes_regime_params.js` (modify) | stop accepting `max_hold_days` | C5 |
| `tests/test_leverage.js` / `test_regime_freshness.js` / `test_pipelines_summary.js` / `test_regime_for_strategy.js` (new) | unit tests | C1–C4 |

---

# PHASE 1 — low-risk display/relabel (land, then STOP for review)

## Task C1: Realized-leverage surface (D6)

**Files:**
- Create: `src/channels/api/leverage.js`
- Create: `tests/test_leverage.js`
- Modify: `src/channels/api/server.js:1939-1950` (account route) + the account panel render (locate `renderAccountRow` / the account tile in the inline client JS, ~6566)

**Interfaces:**
- Produces: `realizedLeverage({long_market_value, short_market_value, equity}) -> {gross: number|null, net: number|null}`

- [ ] **Step 1: Write the failing test** — `tests/test_leverage.js`

```js
// tests/test_leverage.js — realized broker leverage from account market values.
const assert = require('assert');
const { realizedLeverage } = require('../src/channels/api/leverage');

// 1. Long/short book → gross uses absolute values (live audit: 1.698x / 0.243x)
{
  const r = realizedLeverage({ long_market_value: 113302.37, short_market_value: -84914.84, equity: 116734.52 });
  assert.ok(Math.abs(r.gross - 1.6980) < 1e-3, `gross ~1.698 got ${r.gross}`);
  assert.ok(Math.abs(r.net   - 0.2432) < 1e-3, `net ~0.243 got ${r.net}`);
}
// 2. equity <= 0 → null (no div-by-zero / nonsense)
{
  const r = realizedLeverage({ long_market_value: 100, short_market_value: 0, equity: 0 });
  assert.strictEqual(r.gross, null); assert.strictEqual(r.net, null);
}
// 3. Flat book → 0
{
  const r = realizedLeverage({ long_market_value: 0, short_market_value: 0, equity: 1000 });
  assert.strictEqual(r.gross, 0); assert.strictEqual(r.net, 0);
}
// 4. String inputs (Alpaca CLI returns strings) are coerced
{
  const r = realizedLeverage({ long_market_value: '200', short_market_value: '-50', equity: '100' });
  assert.strictEqual(r.gross, 2.5); assert.strictEqual(r.net, 1.5);
}
console.log('ok test_leverage');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node tests/test_leverage.js`
Expected: FAIL — `Cannot find module '../src/channels/api/leverage'`

- [ ] **Step 3: Write minimal implementation** — `src/channels/api/leverage.js`

```js
// src/channels/api/leverage.js
// Realized broker leverage from account market values. Pure; no I/O.
//   gross = (|long| + |short|) / equity  — TRUE exposure (the book is long/short)
//   net   = (long + short) / equity
// Returns nulls when equity is not positive (avoid div-by-zero / misleading values).
function realizedLeverage({ long_market_value, short_market_value, equity } = {}) {
  const lmv = Number(long_market_value) || 0;
  const smv = Number(short_market_value) || 0;
  const eq  = Number(equity) || 0;
  if (!(eq > 0)) return { gross: null, net: null };
  return { gross: (Math.abs(lmv) + Math.abs(smv)) / eq, net: (lmv + smv) / eq };
}
module.exports = { realizedLeverage };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node tests/test_leverage.js`
Expected: `ok test_leverage`

- [ ] **Step 5: Wire into the account route** — `src/channels/api/server.js`

At the top of the route file's requires (near other `require('./...')`), add:
`const { realizedLeverage } = require('./leverage');`
In `app.get('/api/portfolio/account', ...)` (line 1939 `res.json({...})`), add two fields computed from the RAW `a` (string fields are coerced by the helper):

```js
    const _lev = realizedLeverage(a);
    res.json({
      equity:             parseFloat(a.equity)             || 0,
      cash:               parseFloat(a.cash)               || 0,
      buying_power:       parseFloat(a.buying_power)       || 0,
      last_equity:        parseFloat(a.last_equity)        || 0,
      long_market_value:  parseFloat(a.long_market_value)  || 0,
      short_market_value: parseFloat(a.short_market_value) || 0,
      gross_leverage:     _lev.gross,
      net_leverage:       _lev.net,
      day_pnl:           (parseFloat(a.equity) - parseFloat(a.last_equity)) || 0,
      day_pnl_pct:        parseFloat(a.last_equity) > 0
                            ? ((parseFloat(a.equity) - parseFloat(a.last_equity)) / parseFloat(a.last_equity) * 100)
                            : 0,
    });
```

- [ ] **Step 6: Surface in the account/exposure panel** — inline client JS (~`renderAccountRow`, ~6566)

Read the account-tile render block. Add a tile/line that shows the actual leverage from `acct.gross_leverage`, clearly labeled "actual", beside any existing config-intent λ×liq figure (~7460). Representative snippet (adapt to the surrounding template):
```js
const _lev = (acct.gross_leverage == null) ? '—' : acct.gross_leverage.toFixed(2) + '×';
// in the panel markup, add:  <span title="Gross broker exposure (|long|+|short|)/equity">Actual leverage: ${_lev}</span>
```
If a "target leverage" label exists nearby, relabel it "target" so the two aren't confused.

- [ ] **Step 7: Verify + commit (path-scoped)**

```bash
node tests/test_leverage.js
cd /root/openclaw && git add src/channels/api/leverage.js tests/test_leverage.js src/channels/api/server.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/channels/api/leverage.js src/channels/api/server.js tests/test_leverage.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(dashboard): surface realized broker leverage (D6)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task C2: Regime daily-block freshness flag (D5)

**Files:**
- Create: `src/channels/api/regime_freshness.js`
- Create: `tests/test_regime_freshness.js`
- Modify: `src/channels/api/server.js:2440-2449` (`/api/regime`) + the regime gauge render (~5055, `dly.stress_score`/`roro_score`)

**Interfaces:**
- Produces: `regimeFreshness(regimeObj, nowMs) -> {daily_date: string|null, daily_age_hours: number|null, daily_stale: boolean}`

- [ ] **Step 1: Write the failing test** — `tests/test_regime_freshness.js`

```js
// tests/test_regime_freshness.js — flag a stale DAILY regime block (frozen
// date/stress/roro) so the dashboard greys it instead of showing it as current.
const assert = require('assert');
const { regimeFreshness } = require('../src/channels/api/regime_freshness');

// 1. THE LIVE CASE: daily date 2026-06-08 frozen, intraday fresh 2026-06-26 → stale.
{
  const r = regimeFreshness({ date: '2026-06-08', intraday_updated_at: '2026-06-26 23:45:00+00:00' },
                            Date.parse('2026-06-29T04:00:00Z'));
  assert.strictEqual(r.daily_stale, true);
  assert.strictEqual(r.daily_date, '2026-06-08');
  assert.ok(r.daily_age_hours > 24 * 20);
}
// 2. Daily block fresh (same day as intraday) → not stale.
{
  const r = regimeFreshness({ date: '2026-06-29', intraday_updated_at: '2026-06-29 13:00:00+00:00' },
                            Date.parse('2026-06-29T14:00:00Z'));
  assert.strictEqual(r.daily_stale, false);
}
// 3. No intraday field → fall back to absolute age (>48h stale).
{
  const r = regimeFreshness({ date: '2026-06-20' }, Date.parse('2026-06-29T00:00:00Z'));
  assert.strictEqual(r.daily_stale, true);
}
// 4. Garbage / missing → safe defaults, never throws.
{
  assert.deepStrictEqual(regimeFreshness(null, Date.now()), { daily_date: null, daily_age_hours: null, daily_stale: false });
  assert.strictEqual(regimeFreshness({}, Date.now()).daily_stale, false);
}
console.log('ok test_regime_freshness');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node tests/test_regime_freshness.js`
Expected: FAIL — module not found

- [ ] **Step 3: Write minimal implementation** — `src/channels/api/regime_freshness.js`

```js
// src/channels/api/regime_freshness.js
// The daily HMM block (date/stress_score/roro_score) in regime_latest.json can
// freeze (e.g. the 2026-06-08 operator resync) while the intraday block keeps
// updating. This flags that staleness so the UI greys the daily values instead
// of presenting frozen numbers as current. Pure; no I/O.
function regimeFreshness(reg, nowMs) {
  const out = { daily_date: null, daily_age_hours: null, daily_stale: false };
  if (!reg || typeof reg !== 'object') return out;
  const d = reg.date || null;
  out.daily_date = d;
  const dailyMs = d ? Date.parse(d + 'T00:00:00Z') : NaN;
  if (Number.isFinite(dailyMs) && Number.isFinite(nowMs)) {
    out.daily_age_hours = (nowMs - dailyMs) / 3600000;
  }
  const intra = reg.intraday_updated_at
    ? Date.parse(String(reg.intraday_updated_at).replace(' ', 'T')) : NaN;
  if (Number.isFinite(dailyMs) && Number.isFinite(intra)) {
    out.daily_stale = (intra - dailyMs) > 24 * 3600000;        // intraday >1d newer than daily
  } else if (Number.isFinite(out.daily_age_hours)) {
    out.daily_stale = out.daily_age_hours > 48;                // fallback: daily block >2d old
  }
  return out;
}
module.exports = { regimeFreshness };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node tests/test_regime_freshness.js`
Expected: `ok test_regime_freshness`

- [ ] **Step 5: Add freshness to `/api/regime`** — `src/channels/api/server.js:2442-2443`

Add `const { regimeFreshness } = require('./regime_freshness');` near the other requires, then:
```js
    const raw = await fs.promises.readFile(REGIME_FILE, 'utf8');
    const parsed = JSON.parse(raw);
    res.json({ available: true, ...parsed, ...regimeFreshness(parsed, Date.now()) });
```

- [ ] **Step 6: Grey the stale daily block in the gauge** — inline client JS (~5055-5061)

Where `stress` / `roro` / the daily date are rendered from `dly`, when `dly.daily_stale` is true, render them greyed with an "as-of <daily_date> (stale)" caption instead of as current. Representative:
```js
const _stale = dly && dly.daily_stale;
const _asOf  = dly && dly.daily_date ? ` (as-of ${dly.daily_date}${_stale ? ', stale' : ''})` : '';
// apply a muted CSS class to the stress/roro elements when _stale, and append _asOf to their label.
```

- [ ] **Step 7: Verify + commit (path-scoped)**

```bash
node tests/test_regime_freshness.js
cd /root/openclaw && git add src/channels/api/regime_freshness.js tests/test_regime_freshness.js src/channels/api/server.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/channels/api/regime_freshness.js src/channels/api/server.js tests/test_regime_freshness.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(dashboard): flag stale daily regime block (D5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task C3: Pipelines tile durable backfill (D3)

**Files:**
- Create: `src/channels/api/pipelines_summary.js`
- Create: `tests/test_pipelines_summary.js`
- Modify: `src/channels/api/routes_pipelines.js:91-130` (`/summary`)

**Interfaces:**
- Consumes: traceBus run objects + `fetchPersistedRuns()` output (`{threadId, graph, ...}`).
- Produces: `summarizePipelines(liveRuns, persistedRuns, nowMs) -> {active, today, failures_24h, graphs: string[], live_window: 'since_restart'}`

- [ ] **Step 1: Write the failing test** — `tests/test_pipelines_summary.js`

```js
// tests/test_pipelines_summary.js — tile counts must include durable graphs so the
// panel isn't empty after a restart wipes the in-memory traceBus (live bug:
// graphs:[] despite durable_total:58).
const assert = require('assert');
const { summarizePipelines } = require('../src/channels/api/pipelines_summary');
const NOW = Date.parse('2026-06-29T12:00:00Z');

// 1. Empty traceBus + durable graphs present → graphs is NON-empty (the bug fix).
{
  const s = summarizePipelines([], [{ threadId: 't1', graph: 'daily-cycle' }, { threadId: 't2', graph: 'paperhunter' }], NOW);
  assert.deepStrictEqual([...s.graphs].sort(), ['daily-cycle', 'paperhunter']);
  assert.strictEqual(s.active, 0);
  assert.strictEqual(s.live_window, 'since_restart');
}
// 2. Live runs drive active/today/failures; graphs union live+durable, deduped, no 'unknown'.
{
  const live = [
    { status: 'running', startedAt: NOW - 1000, updatedAt: NOW, meta: { graph: 'daily-cycle' } },
    { status: 'error',   startedAt: NOW - 2000, updatedAt: NOW - 1000, meta: { graph: 'x' } },
  ];
  const s = summarizePipelines(live, [{ threadId: 't', graph: 'daily-cycle' }], NOW);
  assert.strictEqual(s.active, 1);
  assert.strictEqual(s.failures_24h, 1);
  assert.strictEqual(s.today, 2);
  assert.deepStrictEqual([...s.graphs].sort(), ['daily-cycle', 'x']);
}
// 3. Old failure outside 24h not counted.
{
  const live = [{ status: 'failed', startedAt: NOW - 5 * 86400000, updatedAt: NOW - 5 * 86400000, meta: { graph: 'g' } }];
  assert.strictEqual(summarizePipelines(live, [], NOW).failures_24h, 0);
}
console.log('ok test_pipelines_summary');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node tests/test_pipelines_summary.js`
Expected: FAIL — module not found

- [ ] **Step 3: Write minimal implementation** — `src/channels/api/pipelines_summary.js`

```js
// src/channels/api/pipelines_summary.js
// Pure tile summarizer. active/today/failures come from live traceBus runs
// ("since this process started" — labeled live_window). graphs is the UNION of
// live + durable (persisted) graph names so the panel isn't empty after a restart.
function graphOf(run) { return run?.meta?.graph || run?.meta?.graphName || run?.graph || 'unknown'; }

function summarizePipelines(liveRuns = [], persistedRuns = [], nowMs = Date.now()) {
  const live = liveRuns || [];
  const active = live.filter((r) => r.status === 'running').length;
  const failures_24h = live.filter((r) => {
    const isFail = r.status === 'error' || r.status === 'failed';
    return isFail && (nowMs - (r.updatedAt || 0) < 24 * 3600000);
  }).length;
  const todayStart = new Date(nowMs); todayStart.setHours(0, 0, 0, 0);
  const today = live.filter((r) => (r.startedAt || 0) >= todayStart.getTime()).length;
  const graphs = [...new Set([...live.map(graphOf), ...(persistedRuns || []).map(graphOf)])]
    .filter((g) => g && g !== 'unknown');
  return { active, today, failures_24h, graphs, live_window: 'since_restart' };
}
module.exports = { summarizePipelines, graphOf };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node tests/test_pipelines_summary.js`
Expected: `ok test_pipelines_summary`

- [ ] **Step 5: Use it in `/summary`** — `src/channels/api/routes_pipelines.js:91-126`

Replace the inline active/today/failures/graphs computation with the helper, reusing the existing `fetchPersistedRuns`. Keep `durable_total`:
```js
const { summarizePipelines } = require('./pipelines_summary');
// ...
router.get('/summary', async (_req, res) => {
  try {
    const liveRuns = traceBus.listRuns();
    let persisted = [];
    try { persisted = await fetchPersistedRuns(200); } catch (_) { /* schema absent; non-fatal */ }
    const base = summarizePipelines(liveRuns, persisted, Date.now());
    let durableTotal = null;
    try {
      const r = await dbQuery(`SELECT COUNT(DISTINCT thread_id) AS n FROM langgraph.checkpoints WHERE checkpoint_ns = ''`);
      durableTotal = Number((r.rows || r)[0].n);
    } catch (_) { /* non-fatal */ }
    res.json({ ...base, durable_total: durableTotal, generated_at: new Date().toISOString() });
  } catch (err) { res.status(500).json({ error: err.message }); }
});
```
(Optional UI step: ensure the tile labels the live counters "since restart" and shows `durable_total`.)

- [ ] **Step 6: Verify + commit (path-scoped)**

```bash
node tests/test_pipelines_summary.js
cd /root/openclaw && git add src/channels/api/pipelines_summary.js tests/test_pipelines_summary.js src/channels/api/routes_pipelines.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/channels/api/pipelines_summary.js src/channels/api/routes_pipelines.js tests/test_pipelines_summary.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(dashboard): backfill pipelines tile from durable checkpoints (D3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task C4: Crypto badge uses the crypto regime (D4)

**Files:**
- Modify: `src/channels/api/regime_active.js` (add `regimeForStrategy`)
- Create: `tests/test_regime_for_strategy.js`
- Modify: `src/channels/api/server.js:1296-1313` (badge loop) — load crypto regime once, branch per strategy

**Interfaces:**
- Produces: `regimeForStrategy(instrumentClass, equityRegime, cryptoRegime) -> string|null`

- [ ] **Step 1: Write the failing test** — `tests/test_regime_for_strategy.js`

```js
// tests/test_regime_for_strategy.js — crypto strategies must be badged against the
// CRYPTO regime (engine gates them on crypto_regime_latest.json), not the equity one.
const assert = require('assert');
const { regimeForStrategy } = require('../src/channels/api/regime_active');

assert.strictEqual(regimeForStrategy('crypto', 'LOW_VOL', 'HIGH_VOL'), 'HIGH_VOL', 'crypto → crypto regime');
assert.strictEqual(regimeForStrategy('equity', 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'equity → equity regime');
assert.strictEqual(regimeForStrategy(undefined, 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'default → equity regime');
assert.strictEqual(regimeForStrategy('etp', 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'non-crypto → equity regime');
assert.strictEqual(regimeForStrategy('crypto', 'LOW_VOL', null), 'LOW_VOL', 'crypto regime missing → equity fallback');
console.log('ok test_regime_for_strategy');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node tests/test_regime_for_strategy.js`
Expected: FAIL — `regimeForStrategy is not a function`

- [ ] **Step 3: Add the selector** — `src/channels/api/regime_active.js` (before `module.exports`)

```js
/**
 * Pick the regime a strategy's badge should be evaluated against. Crypto
 * strategies are gated by the engine on the crypto regime; everything else on
 * the equity regime. Falls back to the equity regime if the crypto one is absent.
 */
function regimeForStrategy(instrumentClass, equityRegime, cryptoRegime) {
  if (instrumentClass === 'crypto') return cryptoRegime || equityRegime || null;
  return equityRegime || null;
}
module.exports = { isRegimeEligibleNow, regimeForStrategy };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node tests/test_regime_for_strategy.js`
Expected: `ok test_regime_for_strategy`

- [ ] **Step 5: Branch the badge loop** — `src/channels/api/server.js:1296-1307`

Before the loop, load the crypto regime once (read `.agents/crypto-market-state/crypto_regime_latest.json`'s `state`, best-effort/null on error — follow how `currentRegime` is loaded for the equity file, ~1252). Import `regimeForStrategy` from `./regime_active`. In the loop replace the eligibility line:
```js
      const _regime = regimeForStrategy(rec.instrument_class, currentRegime, cryptoRegime);
      const regimeActive = isRegimeEligibleNow(regimeParamsById[sid], _regime);
```
Ensure the row's displayed regime for crypto strategies reflects `_regime` (so the badge and the shown regime agree). Confirm `rec.instrument_class` exists on the manifest record (SP-3 added it; if absent it’s `undefined` → equity path, safe).

- [ ] **Step 6: Verify + commit (path-scoped)**

```bash
node tests/test_regime_for_strategy.js && node tests/test_regime_active.js
cd /root/openclaw && git add src/channels/api/regime_active.js tests/test_regime_for_strategy.js src/channels/api/server.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/channels/api/regime_active.js src/channels/api/server.js tests/test_regime_for_strategy.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(dashboard): crypto strategy badge uses crypto regime (D4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task C5: Relabels + dead-panel removals (D7)

Mostly string/markup edits with no unit-test harness (inline client JS) — verify by `grep` + an end-of-phase visual check. Make each edit, then grep-verify, then ONE commit.

**Files:** `src/channels/api/server.js` (multiple), `src/channels/api/routes_regime_params.js`

- [ ] **Step 1: "Trigger news" → honest label.** In `app.post('/api/trigger/news', ...)` (server.js:210) change the JSON response message from "News collection started" to "Old news pruned (>30d)". Find the client button label (grep `trigger/news` in the inline JS) and rename it "Prune old news (>30d)".

- [ ] **Step 2: Watchlist relabel.** Grep the watchlist panel markup (near `/api/watchlist`); add caption "Broker watchlist — does not change the traded universe."

- [ ] **Step 3: regime-priors relabel.** Grep the priors control in the inline JS (POSTs to `/api/regime-priors/`); add caption "Research/diagnostic input — not live sizing."

- [ ] **Step 4: Remove the always-empty Verdicts panel.** Grep `/api/verdicts` in the inline client JS; remove the panel markup + its fetch (leave the route). Confirm no other code references the removed DOM ids (grep them).

- [ ] **Step 5: Drop dead provider columns from db/cycles display.** Grep `polygon_calls` / `yfinance_calls` in the inline render of the cycles table; remove those columns from the displayed table (leave the API/DB).

- [ ] **Step 6: Remove `max_hold_days` from the control surface.** In the regime-params UI (grep `max_hold_days` in server.js inline JS) remove the input. In `routes_regime_params.js` POST handler, stop reading/forwarding `max_hold_days` (drop it from the params passed to `eligibility_manager`); leave the DB column.

- [ ] **Step 7: Verify + commit (path-scoped).**

```bash
cd /root/openclaw
grep -n "News collection started" src/channels/api/server.js && echo "FAIL: old label remains" && exit 1 || echo "label updated"
grep -n "polygon_calls\|yfinance_calls" src/channels/api/server.js | grep -i "render\|<td\|column\|header" || echo "provider cols removed from display"
node -e "require('./src/channels/api/server.js')" 2>&1 | head -3 || true   # syntax sanity (may bind port; Ctrl-C ok) — prefer: node --check
node --check src/channels/api/server.js && node --check src/channels/api/routes_regime_params.js && echo "syntax ok"
git add src/channels/api/server.js src/channels/api/routes_regime_params.js
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/channels/api/routes_regime_params.js src/channels/api/server.js " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(dashboard): relabel/remove dead+misleading controls (D7)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Parallel (read-only): D1c strategy-drift sign-off sheet

Generate `docs/w2-strategy-drift-signoff.md` from live data (the query already prototyped in scratchpad): every strategy where manifest trade-intent ≠ registry trade-reality, columns `id · manifest_state · registry_status · last_signal_date · open_in_real_book · backtest_sharpe · recommended_action`. Deliver at the gate. **No live-book change.**

---

>>> ## OPERATOR REVIEW GATE — STOP HERE <<<
Land C1–C5 + deliver the sign-off sheet. Operator reviews Phase 1 on the running dashboard (after the discrete user-scope `johnbot` restart) and signs off the drift sheet BEFORE Phase 2 begins. Do not start C6 without approval.

---

# PHASE 2 — live-touching (DETAIL AFTER THE GATE)

These tasks touch the live promotion path / DB and will be expanded into full bite-sized TDD steps after Phase 1 review (their exact shape benefits from Phase-1 learnings + the operator's drift decisions). Outline:

- **C6 — `strategy_drift.js` + D1a display drift-flag.** Pure `classifyDrift(manifestState, registryStatus)` + `summarizeDrift(rows)`; LEFT JOIN `strategy_registry.status` into `GET /api/strategies`; `strategy_row.js` renders a ⚠️ drift badge + header count. TDD on the pure helper.
- **C7 — D1b fatal+retried registry sync** in `POST /api/strategies/:id/transition` (server.js ~1722): 3-retry; on persistent failure REVERT the manifest `.state` write under the existing lock and return 500. Full TDD: success / registry-fail→revert / retry-then-succeed.
- **C8 — D2 staging-Approve → real promotion queue:** wire `POST /staging/:id/decision` (approve) to enqueue the existing `research_candidates → strategy_approval_jobs` job (confirm the staging→candidate linkage key first; if none, fall back to relabel + surface).
- **C9 — D8 pipeline_config dedup + `UNIQUE(key)` migration:** inspect the duplicate `collection_enabled`/`collect_technicals` values (flag if they differ), delete redundant rows keeping the correct value, add a numbered migration adding `UNIQUE(key)`, confirm writers use `ON CONFLICT(key)`. Round-trip migration test (apply on seeded dup → one row + constraint; idempotent re-run).

# Deferred (logged, NOT this plan)
U1 P&L fill-reconcile (→ W7) · U2 Jun-8 regime daily-block freeze (root of D5's stale values) · U3 `signal_pnl` 76k stale-open bloat · U4 the per-strategy manifest↔registry trading decision (gated on the C5 sign-off sheet; no auto-sync).

## Self-Review (author)
- **Spec coverage:** D1a/b/c→C6/C7/parallel; D2→C8; D3→C3; D4→C4; D5→C2; D6→C1; D7→C5; D8→C9. All §3–§7 buckets mapped. ✓
- **Placeholders:** Phase 1 steps carry full code/commands. Phase 2 is intentionally an outline behind the gate (not a placeholder for Phase 1). ✓
- **Type consistency:** `realizedLeverage`, `regimeFreshness`, `summarizePipelines`/`graphOf`, `regimeForStrategy` signatures match between their tests, modules, and consumers. ✓
