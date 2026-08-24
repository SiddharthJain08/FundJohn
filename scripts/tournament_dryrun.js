#!/usr/bin/env node
'use strict';

/**
 * scripts/tournament_dryrun.js — Task S2 offline dry-run.
 *
 * Exercises the N-variant strategycoder tournament's winner selection, loser
 * file cleanup, canonical rename, DB re-attribution bookkeeping, and gate-
 * decision emission WITHOUT spawning strategycoder, python (validate_strategy
 * / factor_prescreen / unified_backtest / eligibility_assigner / the
 * winner-loadable check), an LLM (redteamStrategy), or touching the real
 * Postgres / implementations directory. Safe to run any time, including
 * during the live-trade lane.
 *
 * Isolation, in order of what matters most:
 *   1. process.env.OPENCLAW_DIR is redirected to a fresh throwaway temp
 *      directory per scenario, BEFORE research-orchestrator.js is
 *      require()'d (module cache is cleared between scenarios so each one
 *      re-evaluates OPENCLAW_DIR / IMPLEMENTATIONS_DIR / MANIFEST_PATH
 *      against its own scratch dir). Every file the tournament reads,
 *      renames, writes, or deletes lands under that scratch dir — never
 *      under the real repo's src/strategies/implementations/.
 *   2. process.env.POSTGRES_URI is cleared, which engages gate-decisions.js's
 *      own documented no-op guard ("if (!process.env.POSTGRES_URI) return;
 *      // tests / dry runs") for paperIdForCandidate() — belt-and-suspenders,
 *      since (3) below means the real emitGateDecision is never called
 *      either way.
 *   3. orch._emitDecisionFn (the seam _runGateChain / _runTournament call
 *      instead of the bare emitGateDecision import) is overridden to push
 *      into a plain array instead of hitting Postgres — this is what lets
 *      the assertions below inspect exactly what decisions each scenario
 *      produced, including every tournament_loser's metadata.
 *   4. orch._query is overridden to a stub that records calls and returns no
 *      rows — covers the direct _query() calls _runTournament makes for DB
 *      re-attribution and the terminal implementation_queue status write.
 *   5. orch._validateFn / _redteamFn / _prescreenFn / _backtestFn (the four
 *      test seams _runGateChain calls through) and orch._assignEligibility /
 *      _verifyWinnerLoadable are all stubbed, so no subprocess or LLM call is
 *      ever made. The "tiny pre-written variant files" are dropped straight
 *      at the paths _codeVariant expects — its existing skip-if-exists check
 *      means _codeStrategy (the real strategycoder call) is never invoked.
 *
 * Three scenarios, each calling _runTournament() directly (the unit Task S2
 * actually adds — _codeFromQueue's job is just resolving N and branching,
 * which is exercised by node --check + the byte-identical guard grep, not by
 * fixture-based scenarios):
 *   - winner-selected: 3 variants, one is disqualified by the min_trades
 *     floor despite the highest Sharpe, winner is the highest-Sharpe
 *     survivor.
 *   - zero-survivors-floor-miss: every variant backtests cleanly but none
 *     clears the floor -> new 'tournament_no_survivors' terminal reasonCode,
 *     implementation_queue.status='backtest_failed'.
 *   - zero-survivors-all-gates-failed: every variant fails validate ->
 *     reuses variant 1's OWN real single-shot status
 *     ('validation_failed'/'contract_violation'), per the brief's "same
 *     status the single-shot failure path uses today."
 *
 * Usage: node scripts/tournament_dryrun.js
 */

const fs   = require('fs');
const os   = require('os');
const path = require('path');

const RO_PATH = path.join(__dirname, '..', 'src/agent/research/research-orchestrator.js');

function freshOrchestrator() {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'tournament-dryrun-'));
  process.env.OPENCLAW_DIR = scratch;
  delete process.env.POSTGRES_URI;
  const implDir = path.join(scratch, 'src/strategies/implementations');
  fs.mkdirSync(implDir, { recursive: true });
  fs.writeFileSync(path.join(scratch, 'src/strategies/manifest.json'), JSON.stringify({ strategies: {} }, null, 2));
  fs.writeFileSync(path.join(scratch, 'src/strategies/registry.py'), '_IMPL_MAP = {}\n');
  delete require.cache[require.resolve(RO_PATH)];
  // eslint-disable-next-line import/no-dynamic-require
  const ResearchOrchestrator = require(RO_PATH);
  return { scratch, implDir, ResearchOrchestrator };
}

function instrumentedOrchestrator(RO) {
  const orch = new RO();
  const dbCalls   = [];
  const decisions = [];
  orch._query = async (sql, params) => {
    dbCalls.push({ sql: sql.trim().replace(/\s+/g, ' '), params });
    return { rows: [] };
  };
  // The seam this fix adds (review finding): captures every decision
  // _runGateChain / _runTournament would have sent to paper_gate_decisions,
  // in-process, with no Postgres involved.
  orch._emitDecisionFn = async (d) => { decisions.push(d); };
  orch._assignEligibility    = async (stratId, notify) =>
    notify?.(`  [stub] eligibility_assigner skipped for ${stratId} (offline dry-run)`);
  // Review fix: the old _stripImplMapEntry (an in-process registry.py
  // read-modify-writeFileSync) was removed for being unsafe against
  // concurrent readers in other processes. Its replacement,
  // _verifyWinnerLoadable, spawns a REAL python subprocess to check the
  // winner resolves via strategies.registry.load_strategy_class — stubbed
  // here so the dry run stays offline.
  orch._verifyWinnerLoadable = async (stratId, notify) => {
    notify?.(`  [stub] winner-loadable check skipped for ${stratId} (offline dry-run)`);
    return { loadable: true };
  };
  return { orch, dbCalls, decisions };
}

function writeVariantFixture(RO, canonicalPath, k) {
  fs.writeFileSync(RO._variantPath(canonicalPath, k), [
    `# fake strategycoder output for variant ${k} — tournament_dryrun.js fixture`,
    `# (content is never read: _validateFn/_redteamFn/_prescreenFn/_backtestFn are stubbed)`,
    `class DryRunFixtureVariant${k}: pass`,
    '',
  ].join('\n'));
}

// ── Scenario 1: a winner is selected ────────────────────────────────────────
// tv1: survives the min_trades floor (100, equity class), modest Sharpe.
// tv2: WINNER — survives the floor, highest Sharpe.
// tv3: higher Sharpe than the winner but BELOW the floor -> disqualified.
//      Proves the tournament isn't a naive max(sharpe) — it filters on the
//      same min_trades field the CANDIDATE->LIVE promotion gate reads
//      (promotion_service.js PROMOTION_THRESHOLDS) before ranking.
async function scenarioWinner() {
  const { scratch, implDir, ResearchOrchestrator: RO } = freshOrchestrator();
  const STRAT_ID     = 'S_dryrun_fixture';
  const CANDIDATE_ID = 'dryrun-candidate-winner';
  const N            = 3;
  const canonicalPath = path.join(implDir, `${STRAT_ID}.py`);
  const FIXTURES = [
    { k: 1, sharpe: 0.8, trade_count: 150 },
    { k: 2, sharpe: 1.9, trade_count: 120 },
    { k: 3, sharpe: 5.0, trade_count: 40 },
  ];
  for (const f of FIXTURES) writeVariantFixture(RO, canonicalPath, f.k);
  const btByPath = new Map(FIXTURES.map(f => [RO._variantPath(canonicalPath, f.k), {
    sharpe: f.sharpe, max_dd: 0.10, total_return_pct: 12.3, trade_count: f.trade_count,
    hit_rate: 0.55, avg_holding_days: 4.2, regime_breakdown: {}, run_id: `dryrun-run-${f.k}`,
    method: 'unified_backtest_discovery',
  }]));

  const notifyLog = [];
  const notify        = (m) => notifyLog.push(m);
  const channelNotify = (m) => notifyLog.push(`[channel] ${m}`);
  const { orch, dbCalls, decisions } = instrumentedOrchestrator(RO);
  orch._validateFn  = async () => ({ ok: true, signal_count: 42 });
  orch._redteamFn   = async () => ({ verdict: 'pass', findings: [] });
  orch._prescreenFn = async () => ({ psResult: { pass: true, reason: null, stats: {} }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn  = async (implPath) => btByPath.get(implPath) || { error: `dryrun: no fixture for ${implPath}` };

  const strategy_spec = { strategy_id: STRAT_ID, inferred_instrument_class: 'equity' };
  const result = await orch._runTournament({
    candidate_id: CANDIDATE_ID, strategy_spec, stratId: STRAT_ID, N, notify, channelNotify, opts: {},
  });

  const filesAfter    = fs.readdirSync(implDir).sort();
  const okCanonical    = filesAfter.includes(`${STRAT_ID}.py`);
  const noLoserFiles   = !filesAfter.some(f => f.includes('_tv'));
  const winnerRight    = !!(result.ok && result.btResult && result.btResult.sharpe === 1.9 && result.btResult.trade_count === 120);
  const reattributed   = dbCalls.some(c => c.sql.startsWith('UPDATE strategy_backtest_runs SET strategy_id')
    && c.params[0] === STRAT_ID && c.params[1] === 'dryrun-run-2');
  const winnerDecision = decisions.find(d => d.gateName === 'tournament' && d.outcome === 'pass');
  const loserDecisions = decisions.filter(d => d.gateName === 'tournament' && d.outcome === 'reject');
  const winnerDecisionOk = !!(winnerDecision
    && winnerDecision.reasonCode === 'tournament_winner'
    && winnerDecision.metadata.winner_variant === 2
    && winnerDecision.metadata.winner_loadable === true);
  const loserDecisionsOk = loserDecisions.length === 2
    && loserDecisions.every(d => d.reasonCode === 'tournament_loser'
      && Number.isFinite(d.metadata.sharpe) && Number.isFinite(d.metadata.trade_count));

  const checks = {
    'canonical file present':                            okCanonical,
    'no _tv* files remain (losers deleted)':              noLoserFiles,
    'winner is variant 2 (Sharpe 1.9/120 trades, beats':  winnerRight,
    "  tv3's 5.0/40<floor and tv1's 0.8)":                winnerRight,
    "winner's backtest run re-attributed to canonical id": reattributed,
    "winner decision: reasonCode='tournament_winner', winner_variant=2, winner_loadable=true": winnerDecisionOk,
    '2 tournament_loser decisions, each carrying sharpe+trade_count metrics': loserDecisionsOk,
  };

  fs.rmSync(scratch, { recursive: true, force: true });
  return { name: 'winner-selected', result, checks, notifyLog };
}

// ── Scenario 2: zero survivors — every variant backtests, none clears the
//    min_trades floor. No single-shot precedent for this (single-shot never
//    gates on trade count) -> new 'tournament_no_survivors' reasonCode.
async function scenarioFloorMiss() {
  const { scratch, implDir, ResearchOrchestrator: RO } = freshOrchestrator();
  const STRAT_ID     = 'S_dryrun_floor_miss';
  const CANDIDATE_ID = 'dryrun-candidate-floor-miss';
  const N            = 2;
  const canonicalPath = path.join(implDir, `${STRAT_ID}.py`);
  const FIXTURES = [
    { k: 1, sharpe: 1.0, trade_count: 50 },
    { k: 2, sharpe: 2.0, trade_count: 10 },
  ];
  for (const f of FIXTURES) writeVariantFixture(RO, canonicalPath, f.k);
  const btByPath = new Map(FIXTURES.map(f => [RO._variantPath(canonicalPath, f.k), {
    sharpe: f.sharpe, max_dd: 0.10, total_return_pct: 1.0, trade_count: f.trade_count,
    hit_rate: 0.5, avg_holding_days: 1, regime_breakdown: {}, run_id: `dryrun-run-fm-${f.k}`,
    method: 'unified_backtest_discovery',
  }]));

  const notifyLog = [];
  const notify = (m) => notifyLog.push(m);
  const { orch, dbCalls, decisions } = instrumentedOrchestrator(RO);
  orch._validateFn  = async () => ({ ok: true });
  orch._redteamFn   = async () => ({ verdict: 'pass', findings: [] });
  orch._prescreenFn = async () => ({ psResult: { pass: true, reason: null, stats: {} }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn  = async (implPath) => btByPath.get(implPath) || { error: 'dryrun: no fixture' };

  const strategy_spec = { strategy_id: STRAT_ID, inferred_instrument_class: 'equity' };
  const result = await orch._runTournament({
    candidate_id: CANDIDATE_ID, strategy_spec, stratId: STRAT_ID, N, notify, channelNotify: () => {}, opts: {},
  });

  const filesAfter    = fs.readdirSync(implDir);
  const queueUpdate   = dbCalls.find(c => c.sql.startsWith('UPDATE implementation_queue'));
  const loserDecisions = decisions.filter(d => d.gateName === 'tournament' && d.outcome === 'reject');

  const checks = {
    'result.ok === false':                             result.ok === false,
    "reasonCode === 'tournament_no_survivors' (no single-shot precedent for a trade-count gate)":
      result.result.reasonCode === 'tournament_no_survivors',
    "implementation_queue.status written = 'backtest_failed'":
      !!(queueUpdate && queueUpdate.params[0] === 'backtest_failed'),
    'all variant files deleted (0 remain)':             filesAfter.length === 0,
    '2 tournament_loser decisions, uniform reasonCode, each carrying sharpe+trade_count':
      loserDecisions.length === 2 && loserDecisions.every(d => d.reasonCode === 'tournament_loser'
        && Number.isFinite(d.metadata.sharpe) && Number.isFinite(d.metadata.trade_count)),
  };

  fs.rmSync(scratch, { recursive: true, force: true });
  return { name: 'zero-survivors-floor-miss', result, checks, notifyLog };
}

// ── Scenario 3: zero survivors — every variant fails validate before any
//    backtest runs. Must reuse variant 1's OWN real single-shot-shaped
//    failure, per "the queue row fails with the same status the single-shot
//    failure path uses today."
async function scenarioAllGatesFailed() {
  const { scratch, implDir, ResearchOrchestrator: RO } = freshOrchestrator();
  const STRAT_ID     = 'S_dryrun_all_fail';
  const CANDIDATE_ID = 'dryrun-candidate-all-fail';
  const N            = 2;
  const canonicalPath = path.join(implDir, `${STRAT_ID}.py`);
  for (const k of [1, 2]) writeVariantFixture(RO, canonicalPath, k);

  const notifyLog = [];
  const notify = (m) => notifyLog.push(m);
  const { orch, dbCalls, decisions } = instrumentedOrchestrator(RO);
  orch._validateFn  = async () => ({ ok: false, errors: ['fake contract violation'] });
  orch._redteamFn   = async () => ({ verdict: 'pass', findings: [] });
  orch._prescreenFn = async () => ({ psResult: { pass: true, reason: null, stats: {} }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn  = async () => ({ error: 'dryrun: should never be reached — validate must reject first' });

  const strategy_spec = { strategy_id: STRAT_ID, inferred_instrument_class: 'equity' };
  const result = await orch._runTournament({
    candidate_id: CANDIDATE_ID, strategy_spec, stratId: STRAT_ID, N, notify, channelNotify: () => {}, opts: {},
  });

  const filesAfter        = fs.readdirSync(implDir);
  const queueUpdate       = dbCalls.find(c => c.sql.startsWith('UPDATE implementation_queue'));
  const validateDecisions = decisions.filter(d => d.gateName === 'validate');
  const loserDecisions    = decisions.filter(d => d.gateName === 'tournament' && d.outcome === 'reject');

  const checks = {
    'result.ok === false':                                                 result.ok === false,
    "reasonCode === 'contract_violation' (variant 1's real single-shot status, not an invented one)":
      result.result.reasonCode === 'contract_violation',
    "implementation_queue.status written = 'validation_failed'":
      !!(queueUpdate && queueUpdate.params[0] === 'validation_failed'),
    'all variant files deleted (0 remain)':                                filesAfter.length === 0,
    'both variants still individually recorded a validate/reject decision (per-variant audit trail preserved)':
      validateDecisions.length === 2 && validateDecisions.every(d => d.outcome === 'reject' && d.reasonCode === 'contract_violation'),
    "2 tournament_loser decisions, uniform reasonCode, gate_failure_reason='contract_violation' in metadata":
      loserDecisions.length === 2 && loserDecisions.every(d => d.reasonCode === 'tournament_loser'
        && d.metadata.gate_failure_reason === 'contract_violation'),
  };

  fs.rmSync(scratch, { recursive: true, force: true });
  return { name: 'zero-survivors-all-gates-failed', result, checks, notifyLog };
}

async function main() {
  const scenarios = [
    await scenarioWinner(),
    await scenarioFloorMiss(),
    await scenarioAllGatesFailed(),
  ];

  let allPass = true;
  for (const s of scenarios) {
    console.log(`\n=== scenario: ${s.name} ===`);
    console.log('notify log:');
    console.log(s.notifyLog.map(l => `  ${l}`).join('\n'));
    console.log('result:', JSON.stringify(s.result));
    for (const [desc, ok] of Object.entries(s.checks)) {
      console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${desc}`);
      if (!ok) allPass = false;
    }
  }

  console.log(`\n${allPass ? 'DRY RUN OK' : 'DRY RUN FAILED'}`);
  process.exit(allPass ? 0 : 1);
}

main().catch((e) => {
  console.error('FATAL', e);
  process.exit(1);
});
