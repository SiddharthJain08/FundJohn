'use strict';

/**
 * D3 — the orchestrator's zero-signal branch (spec 2026-09-12 §4 D3).
 *
 * When validate_strategy reports warnings ['zero_signals_synthetic'], the
 * skip-the-red-team behaviour is gated behind OPENCLAW_ZERO_SIGNAL_SKIP_REDTEAM
 * (fix round 1, spec §0 CRITICAL item):
 *
 *   - flag UNSET (default): the red-team LLM runs exactly as it did before
 *     this task ever landed. The validate-pass decision still records
 *     metadata.warnings + signal_count either way.
 *   - flag SET ('1'): the landed skip-and-continue behaviour — the Opus
 *     reviewer is never called, a `redteam`/`pass`/`needs_signal_check` row
 *     is emitted instead, and the chain keeps going (prescreen + backtest
 *     still run). It must never return ok:false.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     nice -n 19 node --test tests/agent/test_zero_signal_gate.test.js
 */

// paperIdForCandidate() and emitGateDecision() both short-circuit to null when
// POSTGRES_URI is absent, so no pg connection is ever attempted.
delete process.env.POSTGRES_URI;

const os   = require('os');
const fs   = require('fs');
const path = require('path');
const { test, after } = require('node:test');
const assert           = require('node:assert/strict');

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
after(() => { fs.rmSync(FIXTURE_DIR, { recursive: true, force: true }); });

// Fix round 1: the skip is gated behind this flag. Read at call time by the
// orchestrator's `_zeroSignalSkipEnabled()`, so tests can flip it per-test —
// save/restore around each test rather than leaking state to the next one.
const FLAG_KEY = 'OPENCLAW_ZERO_SIGNAL_SKIP_REDTEAM';

function withFlag(value, fn) {
  return async () => {
    const prev = process.env[FLAG_KEY];
    if (value === undefined) delete process.env[FLAG_KEY];
    else process.env[FLAG_KEY] = value;
    try {
      await fn();
    } finally {
      if (prev === undefined) delete process.env[FLAG_KEY];
      else process.env[FLAG_KEY] = prev;
    }
  };
}

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

test('flag unset (default): zero_signals_synthetic warning does NOT skip the red-team LLM', withFlag(undefined, async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  const notified = [];
  const out = await orch._runGateChain({ ...ARGS, notify: (m) => notified.push(m) });

  assert.equal(calls.redteam, 1, 'flag unset (default) — the red-team LLM must still run, byte-identical to pre-D3 behaviour');
  assert.equal(calls.prescreen, 1, 'the prescreen still runs');
  assert.equal(calls.backtest, 1, 'the backtest still runs');
  assert.equal(out.ok, true);

  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1, 'exactly one plain redteam pass row');
  assert.equal(rt[0].outcome, 'pass');
  assert.equal(rt[0].reasonCode, undefined, 'no needs_signal_check row when the flag is unset');

  assert.ok(!notified.some(m => m.includes('needs_signal_check')), 'no ⚠️ needs_signal_check line when the flag is unset');
  assert.equal(notified.filter(m => m.includes('red-team review passed')).length, 1,
    'the "passed" line still posts exactly once — flag unset is byte-identical to pre-D3 behaviour');

  const v = decisions.find(d => d.gateName === 'validate');
  assert.deepEqual(v.metadata.warnings, ['zero_signals_synthetic'], 'the validate decision still records the warning even with the flag unset');
  assert.equal(v.metadata.signal_count, 0);
}));

test('flag set: zero_signals_synthetic warning skips the red-team LLM and marks needs_signal_check', withFlag('1', async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  const notified = [];
  const out = await orch._runGateChain({ ...ARGS, notify: (m) => notified.push(m) });

  assert.equal(calls.redteam, 0, 'the Opus red-team turn must be skipped');
  assert.equal(calls.prescreen, 1, 'the prescreen still runs');
  assert.equal(calls.backtest, 1, 'the backtest still runs — the warning never blocks');
  assert.equal(out.ok, true);

  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1, 'exactly one redteam decision, not two');
  assert.equal(rt[0].outcome, 'pass');
  assert.equal(rt[0].reasonCode, 'needs_signal_check');

  const warnLines   = notified.filter(m => m.includes('needs_signal_check'));
  const passedLines = notified.filter(m => m.includes('red-team review passed'));
  assert.equal(warnLines.length, 1, 'the ⚠️ needs_signal_check line is posted exactly once');
  assert.equal(passedLines.length, 0, 'the "red-team review passed" line must NOT be posted when the red-team was skipped');

  const v = decisions.find(d => d.gateName === 'validate');
  assert.deepEqual(v.metadata.warnings, ['zero_signals_synthetic'], 'the validate decision records the warning with the flag set too');
  assert.equal(v.metadata.signal_count, 0);
}));

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

test('ok:false with warnings: zero_signals_synthetic never reaches the red-team branch', async () => {
  const orch = new ResearchOrchestrator();
  const calls = { redteam: 0 };
  const decisions = [];
  orch._query = async () => ({ rows: [] });
  // Defensive case: even if a future validate_strategy.py shape carries
  // both ok:false and a warnings array, the pre-existing failure path
  // (":1380-ish", before the zero-signal branch is ever read) must return
  // first — the red-team is never called and no needs_signal_check row is
  // ever emitted for a candidate that already failed contract validation.
  orch._validateFn = async () => ({ ok: false, errors: ['boom'], signal_count: 0, warnings: ['zero_signals_synthetic'] });
  orch._redteamFn = async () => { calls.redteam += 1; return { verdict: 'pass', findings: [], infra_fail: false }; };
  orch._prescreenFn = async () => ({ psResult: { pass: true }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn = async () => ({ run_id: 'r1' });
  orch._emitDecisionFn = async (d) => { decisions.push(d); };

  const out = await orch._runGateChain({ ...ARGS });

  assert.equal(calls.redteam, 0, 'ok:false must short-circuit before the red-team branch is ever reached');
  assert.equal(out.ok, false);
  assert.equal(out.result.reasonCode, 'contract_violation');

  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 0, 'no redteam decision at all — and definitely no needs_signal_check row');
});
