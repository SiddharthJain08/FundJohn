'use strict';

/**
 * D3 — the orchestrator's zero-signal branch (spec 2026-09-12 §4 D3).
 *
 * When validate_strategy reports warnings ['zero_signals_synthetic'], the gate
 * chain must (a) NOT call the Opus red-team reviewer, (b) emit a redteam
 * decision with reasonCode 'needs_signal_check', and (c) keep going — the
 * prescreen and the backtest still run. It must never return ok:false.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     node --test tests/agent/test_zero_signal_gate.test.js
 */

// paperIdForCandidate() and emitGateDecision() both short-circuit to null when
// POSTGRES_URI is absent, so no pg connection is ever attempted.
delete process.env.POSTGRES_URI;

const os   = require('os');
const fs   = require('fs');
const path = require('path');
const { test } = require('node:test');
const assert    = require('node:assert/strict');

const ResearchOrchestrator = require('../../src/agent/research/research-orchestrator');

// `_runGateChain` runs a REAL `strategy_lint.py` AST pre-flight (QD Stream E
// Task 9, landed after this brief was written) before it ever calls the
// `_validateFn` seam — see `research-orchestrator.js` ~:1305-1331. It spawns
// python against `implPath` and reads the file from disk, so a nonexistent
// path (the brief's literal `/tmp/S_zero.py`) fails lint with an `io`
// violation before this task's branch is ever reached, and every test below
// would fail for the wrong reason. Point `implPath` at a real, lint-clean
// stub file instead — its content is otherwise irrelevant because
// `_validateFn` is fully stubbed per test.
const FIXTURE_DIR  = fs.mkdtempSync(path.join(os.tmpdir(), 'zsig-'));
const FIXTURE_PATH = path.join(FIXTURE_DIR, 'S_zero.py');
fs.writeFileSync(FIXTURE_PATH, '"""stub strategy file for gate-chain test isolation (D3 T2)."""\n');

function makeOrch({ warnings = [], redteamVerdict = 'pass' } = {}) {
  const orch = new ResearchOrchestrator();
  const calls = { redteam: 0, prescreen: 0, backtest: 0 };
  const decisions = [];
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: warnings.length ? 0 : 7, warnings });
  orch._redteamFn = async () => { calls.redteam += 1; return { verdict: redteamVerdict, findings: [], infra_fail: false }; };
  orch._prescreenFn = async () => { calls.prescreen += 1; return { psResult: { pass: true, reason: null, stats: {} }, psInfraFail: false, psInfraReason: null }; };
  orch._backtestFn = async () => { calls.backtest += 1; return { run_id: 'r1', sharpe: 0.5 }; };
  orch._emitDecisionFn = async (d) => { decisions.push(d); };
  return { orch, calls, decisions };
}

const ARGS = {
  candidate_id: 'cand-1',
  stratId: 'S_zero',
  implPath: FIXTURE_PATH,
  strategy_spec: {},
  opts: {},
  suppressQueueWrite: true,
  runEligibility: false,
};

test('zero_signals_synthetic: red-team LLM is skipped and the chain continues', async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  const out = await orch._runGateChain({ ...ARGS });

  assert.equal(calls.redteam, 0, 'the Opus red-team turn must be skipped');
  assert.equal(calls.prescreen, 1, 'the prescreen still runs');
  assert.equal(calls.backtest, 1, 'the backtest still runs — the warning never blocks');
  assert.equal(out.ok, true);

  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1, 'exactly one redteam decision, not two');
  assert.equal(rt[0].outcome, 'pass');
  assert.equal(rt[0].reasonCode, 'needs_signal_check');
});

test('zero_signals_synthetic: the validate decision carries the warnings list', async () => {
  const { orch, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  await orch._runGateChain({ ...ARGS });
  const v = decisions.find(d => d.gateName === 'validate');
  assert.deepEqual(v.metadata.warnings, ['zero_signals_synthetic']);
  assert.equal(v.metadata.signal_count, 0);
});

test('no warnings: the red-team reviewer runs exactly as before', async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: [] });
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.redteam, 1);
  assert.equal(out.ok, true);
  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1);
  assert.equal(rt[0].reasonCode, undefined, 'the normal pass emit carries no reasonCode');
  assert.deepEqual(rt[0].metadata, { findings: [] });
});

test('a validate result with no warnings key is treated as no warnings', async () => {
  const orch = new ResearchOrchestrator();
  let redteamCalls = 0;
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: 3 });  // legacy shape
  orch._redteamFn = async () => { redteamCalls += 1; return { verdict: 'pass', findings: [], infra_fail: false }; };
  orch._prescreenFn = async () => ({ psResult: { pass: true }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn = async () => ({ run_id: 'r1' });
  orch._emitDecisionFn = async () => {};
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(redteamCalls, 1);
  assert.equal(out.ok, true);
});
