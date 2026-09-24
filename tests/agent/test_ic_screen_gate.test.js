'use strict';

/**
 * D1 — the IC screen's gate-chain wiring (spec 2026-09-12 §4 D1, Task 8).
 *
 * Flag OFF/unset (default = shadow): the verdict is computed, recorded and
 * logged; the backtest ALWAYS runs, no candidate's fate changes. Flag ON
 * (exactly '1'): only verdict 'flat' skips the backtest; 'weak' and
 * 'skipped' annotate and continue in BOTH modes. Infra failure (non-zero
 * exit, timeout, non-shape stdout, a null verdict) mirrors
 * `prescreen_infra_fail` verbatim: warn and pass.
 *
 * Two layers of coverage:
 *   1. Gate-chain level (`_runGateChain`) — `_icScreenFn` is stubbed, so no
 *      python is spawned for the IC screen itself. `_isIcScreenShape` gates
 *      what research.factor_ic_screen's stdout is trusted as.
 *   2. `_runIcScreen` unit level — called DIRECTLY (bypassing
 *      `_runGateChain` and its real `strategy_lint.py` AST pre-flight) with
 *      an injectable `spawnFn` stub (mirrors `_generateTearsheet`'s spawnFn
 *      — see test_tearsheet_hook.test.js), to prove the exit-code / parse /
 *      shape-guard plumbing itself, still without spawning a real python3
 *      process.
 *
 * `_runGateChain` runs a REAL `strategy_lint.py` AST pre-flight (QD Stream E
 * Task 9) before it ever calls the `_validateFn` seam — see
 * research-orchestrator.js's Phase 1. It spawns python against `implPath`
 * and reads the file from disk, so a nonexistent path fails lint with an
 * `io` violation before this task's IC-screen branch is ever reached. Point
 * `implPath` at a real, lint-clean stub file instead (mirrors
 * test_zero_signal_gate.test.js) — its content is otherwise irrelevant
 * because every other gate-chain seam is fully stubbed per test. This is
 * the one real subprocess spawn in this suite (the lint AST walk, not the
 * IC screen); it is inherited from `_runGateChain` itself and has no test
 * seam today.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     nice -n 19 node --test tests/agent/test_ic_screen_gate.test.js
 */

delete process.env.POSTGRES_URI;

const os   = require('os');
const fs   = require('fs');
const path = require('path');
const { test, after } = require('node:test');
const assert           = require('node:assert/strict');

const ResearchOrchestrator = require('../../src/agent/research/research-orchestrator');
const { _isIcScreenShape, QUEUE_STATUS_FOR_REASON } =
  require('../../src/agent/research/research-orchestrator');

const FIXTURE_DIR  = fs.mkdtempSync(path.join(os.tmpdir(), 'icscreen-'));
const FIXTURE_PATH = path.join(FIXTURE_DIR, 'S_ic.py');
fs.writeFileSync(FIXTURE_PATH, '"""stub strategy file for gate-chain test isolation (Task 8)."""\n');
after(() => { fs.rmSync(FIXTURE_DIR, { recursive: true, force: true }); });

const ARGS = {
  candidate_id: 'cand-ic',
  stratId: 'S_ic',
  implPath: FIXTURE_PATH,
  strategy_spec: {},
  opts: {},
  suppressQueueWrite: true,
  runEligibility: false,
};

function makeOrch(icOutcome) {
  const orch = new ResearchOrchestrator();
  const calls = { backtest: 0, icScreen: 0 };
  const decisions = [];
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: 12, warnings: [] });
  orch._redteamFn = async () => ({ verdict: 'pass', findings: [], infra_fail: false });
  orch._prescreenFn = async () => ({ psResult: { pass: true, reason: null, stats: {} },
                                     psInfraFail: false, psInfraReason: null });
  orch._icScreenFn = async () => { calls.icScreen += 1; return icOutcome; };
  orch._backtestFn = async () => { calls.backtest += 1; return { run_id: 'r1', sharpe: 0.4 }; };
  orch._emitDecisionFn = async (d) => { decisions.push(d); };
  return { orch, calls, decisions };
}

// Every fixture below carries the full field set the amendment from the
// Task 7 review requires (`coverage`, `one_sided`, `n_rebalances`), matching
// the ACTUAL JSON keys research/factor_ic_screen.py emits (n_rebalances —
// NOT n_qualifying_rebalances, which is a separate, secondary key).

const FLAT  = { icResult: { verdict: 'flat', reason: 'ic_below_noise_and_ls_below_cost',
                            ic: { '5': 0.002 }, icir: { '5': 0.05 }, ls_q5q1: 0.0001,
                            cost_per_rebalance: 0.0018, turnover: 0.9,
                            coverage: 0.95, one_sided: false, n_rebalances: 40 },
                icInfraFail: false, icInfraReason: null };
const WEAK  = { icResult: { verdict: 'weak', reason: 'icir_below_threshold',
                            ic: { '5': 0.03 }, icir: { '5': 0.11 }, ls_q5q1: 0.004,
                            cost_per_rebalance: 0.0018, turnover: 0.9,
                            coverage: 0.9, one_sided: false, n_rebalances: 38 },
                icInfraFail: false, icInfraReason: null };
const SKIP  = { icResult: { verdict: 'skipped', reason: 'insufficient_cross_section',
                            ic: { '5': null }, icir: { '5': null }, ls_q5q1: null,
                            cost_per_rebalance: 0.002, turnover: null,
                            coverage: 0.2, one_sided: true, n_rebalances: 8 },
                icInfraFail: false, icInfraReason: null };
const PASS  = { icResult: { verdict: 'pass', reason: null, ic: { '5': 0.06 },
                            icir: { '5': 0.9 }, ls_q5q1: 0.01,
                            cost_per_rebalance: 0.0018, turnover: 0.9,
                            coverage: 0.85, one_sided: false, n_rebalances: 42 },
                icInfraFail: false, icInfraReason: null };
// No coverage/one_sided/n_rebalances at all — proves _isIcScreenShape still
// accepts older/partial output and the log line degrades to 'n/a' rather
// than throwing.
const MINIMAL_PASS = { icResult: { verdict: 'pass', reason: null,
                                    ic: { '5': 0.06 }, icir: { '5': 0.9 }, ls_q5q1: 0.01 },
                       icInfraFail: false, icInfraReason: null };
// A real, non-zero NEGATIVE ic/icir — the thresholds are sign-agnostic and a
// negative number must never be logged/emitted as a warning.
const NEG_WEAK = { icResult: { verdict: 'weak', reason: 'icir_below_threshold',
                               ic: { '5': -0.03 }, icir: { '5': -0.11 }, ls_q5q1: -0.004,
                               cost_per_rebalance: 0.0018, turnover: 0.9,
                               coverage: 0.9, one_sided: false, n_rebalances: 38 },
                   icInfraFail: false, icInfraReason: null };

// ── shape guard ───────────────────────────────────────────────────────────────

test('_isIcScreenShape accepts each of the four verdicts', () => {
  for (const v of ['pass', 'weak', 'flat', 'skipped']) {
    assert.equal(_isIcScreenShape({ verdict: v }), true, v);
  }
});

test('_isIcScreenShape accepts a full result carrying coverage/one_sided/n_rebalances', () => {
  assert.equal(_isIcScreenShape(FLAT.icResult), true);
});

test('_isIcScreenShape accepts a minimal result missing coverage/one_sided/n_rebalances', () => {
  assert.equal(_isIcScreenShape(MINIMAL_PASS.icResult), true);
});

test('_isIcScreenShape rejects everything that is not a verdict object', () => {
  for (const bad of [null, undefined, 5, 'flat', [], {}, { verdict: 'blocked' },
                     { verdict: 1 }, [{ verdict: 'flat' }]]) {
    assert.equal(_isIcScreenShape(bad), false, JSON.stringify(bad));
  }
});

test('_isIcScreenShape rejects the module\'s own infra-failure shape (null verdict, landed 10f20c73)', () => {
  assert.equal(_isIcScreenShape({ verdict: null, reason: 'ic_screen_infra_fail', error: 'boom' }), false);
});

// ── flag OFF (shadow) ─────────────────────────────────────────────────────────

test('flag unset: a flat verdict still runs the backtest and is recorded', async (t) => {
  delete process.env.OPENCLAW_IC_SCREEN;
  const { orch, calls, decisions } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.icScreen, 1);
  assert.equal(calls.backtest, 1, 'shadow mode must never skip the backtest');
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_flat');
  assert.equal(ic.metadata.enforced, false);
  assert.equal(ic.metadata.ic_screen.verdict, 'flat');
});

test('flag unset: the [ic_screen] log line carries every required field, once', async () => {
  delete process.env.OPENCLAW_IC_SCREEN;
  const { orch } = makeOrch(WEAK);
  const notified = [];
  await orch._runGateChain({ ...ARGS, notify: (m) => notified.push(m) });
  const lines = notified.filter(m => m.includes('[ic_screen]'));
  assert.equal(lines.length, 1, 'exactly one [ic_screen] line');
  const line = lines[0];
  for (const field of ['verdict=weak', 'reason=icir_below_threshold', 'ic5=0.0300',
                        'icir5=0.1100', 'ls_q5q1=0.0040', 'turnover=0.9000',
                        'coverage=0.9000', 'one_sided=false', 'n_rebalances=38']) {
    assert.ok(line.includes(field), `[ic_screen] line missing ${field}: ${line}`);
  }
});

test('flag unset: a MINIMAL result (no coverage/one_sided/n_rebalances) still logs cleanly as n/a', async () => {
  delete process.env.OPENCLAW_IC_SCREEN;
  const { orch, calls } = makeOrch(MINIMAL_PASS);
  const notified = [];
  const out = await orch._runGateChain({ ...ARGS, notify: (m) => notified.push(m) });
  assert.equal(out.ok, true);
  assert.equal(calls.backtest, 1);
  const line = notified.find(m => m.includes('[ic_screen]'));
  assert.ok(line, 'a [ic_screen] line was still logged');
  assert.ok(line.includes('coverage=n/a'), line);
  assert.ok(line.includes('one_sided=n/a'), line);
  assert.ok(line.includes('n_rebalances=n/a'), line);
});

test('flag unset: negative ic/icir is never logged as a warning', async () => {
  delete process.env.OPENCLAW_IC_SCREEN;
  const { orch } = makeOrch(NEG_WEAK);
  const notified = [];
  await orch._runGateChain({ ...ARGS, notify: (m) => notified.push(m) });
  const icLines = notified.filter(m => m.includes('ic_screen') || m.includes('IC screen'));
  assert.ok(icLines.some(m => m.includes('[ic_screen]')), 'the plain log line was posted');
  assert.ok(!icLines.some(m => m.includes('⚠️')), 'a negative ic/icir must not trigger a warning line');
  assert.ok(!icLines.some(m => m.includes('❌')), 'a negative ic/icir must not block anything');
});

// ── flag ON ───────────────────────────────────────────────────────────────────

test('flag set: a flat verdict skips the backtest and returns ic_screen_flat', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 0, 'the ~900s backtest must be skipped');
  assert.equal(out.ok, false);
  assert.equal(out.result.reasonCode, 'ic_screen_flat');
  assert.match(out.result.error, /ic_below_noise_and_ls_below_cost/);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'reject');
  assert.equal(ic.reasonCode, 'ic_screen_flat');
});

test('flag set: weak annotates and still backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(WEAK);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_weak');
});

test('flag set: skipped (thin cross-section / one-sided) never blocks a decile strategy', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(SKIP);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  assert.equal(decisions.find(d => d.gateName === 'ic_screen').reasonCode, 'ic_screen_skipped');
});

test('flag set: pass annotates and backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(PASS);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  assert.equal(decisions.find(d => d.gateName === 'ic_screen').reasonCode, 'ic_screen_pass');
});

// ── flag strictness ─────────────────────────────────────────────────────────

test('flag set to "true" (not "1") does NOT enforce — a flat verdict still backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = 'true';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
});

test('flag set to "0" does NOT enforce — a flat verdict still backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '0';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
});

// ── infra failure ─────────────────────────────────────────────────────────────

test('infra failure warns and passes through, exactly like the prescreen', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(
    { icResult: null, icInfraFail: true, icInfraReason: 'exit=1' });
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_infra_fail');
});

test('the flat reasonCode maps to a real implementation_queue status', () => {
  assert.equal(QUEUE_STATUS_FOR_REASON.ic_screen_flat, 'ic_screen_flat');
});

// ── _runIcScreen unit level (injectable spawnFn — no python, no lint) ────────

test('_runIcScreen: exit=1 with the module\'s own infra-failure JSON line is an infra failure', async () => {
  const orch = new ResearchOrchestrator();
  const stdout = JSON.stringify({ verdict: null, reason: 'ic_screen_infra_fail', error: 'boom' }) + '\n';
  const spawnStub = async () => ({ stdout, stderr: 'ic screen infra error: boom', code: 1 });
  const { icResult, icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icResult, null);
  assert.equal(icInfraFail, true);
  assert.match(icInfraReason, /exit=1/);
});

test('_runIcScreen: exit=0 with a null-verdict line is STILL an infra failure (shape guard)', async () => {
  const orch = new ResearchOrchestrator();
  const stdout = JSON.stringify({ verdict: null, reason: 'ic_screen_infra_fail', error: 'weird' }) + '\n';
  const spawnStub = async () => ({ stdout, stderr: '', code: 0 });
  const { icResult, icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icResult, null);
  assert.equal(icInfraFail, true);
  assert.match(icInfraReason, /not a valid screen result shape/);
});

test('_runIcScreen: exit=0 with unparseable stdout is an infra failure', async () => {
  const orch = new ResearchOrchestrator();
  const spawnStub = async () => ({ stdout: 'not json at all', stderr: '', code: 0 });
  const { icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icInfraFail, true);
  assert.match(icInfraReason, /unparseable stdout/);
});

test('_runIcScreen: a killed/timed-out child (code null, SIGTERM) is an infra failure', async () => {
  const orch = new ResearchOrchestrator();
  const spawnStub = async () => ({ stdout: '', stderr: '', code: null, signal: 'SIGTERM' });
  const { icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icInfraFail, true);
  assert.match(icInfraReason, /exit=null/);
});

test('_runIcScreen: a throwing spawnFn is an infra failure, never throws out', async () => {
  const orch = new ResearchOrchestrator();
  const spawnStub = async () => { throw new Error('wedged'); };
  await assert.doesNotReject(orch._runIcScreen(FIXTURE_PATH, {}, spawnStub));
  const { icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icInfraFail, true);
  assert.match(icInfraReason, /threw: wedged/);
});

test('_runIcScreen: exit=0 with a valid pass verdict parses cleanly, no infra failure', async () => {
  const orch = new ResearchOrchestrator();
  const stdout = JSON.stringify(PASS.icResult) + '\n';
  const spawnStub = async () => ({ stdout, stderr: '', code: 0 });
  const { icResult, icInfraFail, icInfraReason } = await orch._runIcScreen(FIXTURE_PATH, {}, spawnStub);
  assert.equal(icInfraFail, false);
  assert.equal(icInfraReason, null);
  assert.equal(icResult.verdict, 'pass');
  assert.equal(icResult.n_rebalances, 42);
});
