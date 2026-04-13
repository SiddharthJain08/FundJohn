#!/usr/bin/env node
'use strict';

/**
 * scripts/pipeline-runner.js — Full Pipeline with Phase-Based Concurrency
 *
 * Per-scan execution (3 phases, then repeat on --interval):
 *   Phase 1: Diligence    — concurrency=3, all tickers (verdict-cache KILLs skipped)
 *   Phase 2: Scenario Lab — concurrency=2, PROCEED tickers only
 *   Phase 3: Trade        — sequential,    non-KILL tickers, PROCEED first
 *
 * Optimizations vs v1:
 *   - Phase-based parallelism:  55 tickers × 25min / 3 = ~8h vs 23h sequential
 *   - Verdict cache:            KILL tickers skip 24h / 48h / 96h on consecutive kills
 *   - Scenario gating:          REVIEW tickers never enter scenario lab
 *   - PROCEED-first ordering:   hottest signals reach trade phase first
 *
 * Progress stdout: RUNNER_PROGRESS:{json}
 * Also bubbles BOTJOHN_PROGRESS and TRADE_PROGRESS from child scripts.
 *
 * Usage:
 *   node pipeline-runner.js --tickers AAPL,MSFT --hours 4 [--interval 30] [--max-cost 50]
 *   node pipeline-runner.js --tickers ALL        --hours 4
 *   node pipeline-runner.js --tickers Technology --hours 4
 */

const { spawn } = require('child_process');
const fs        = require('fs');
const path      = require('path');
const budget    = require('./token-budget');

// ── Config ───────────────────────────────────────────────────────────────────
const CLAUDE_BIN  = process.env.CLAUDE_BIN  || '/usr/local/bin/claude-bin';
const CLAUDE_UID  = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID  = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';
const WORKDIR     = process.env.OPENCLAW_DIR || '/root/openclaw';

const MEMOS_DIR     = path.join(WORKDIR, 'output', 'memos');
const UNIVERSE      = path.join(WORKDIR, 'output', 'universe.json');
const VERDICT_CACHE = path.join(WORKDIR, 'output', 'verdict-cache.json');

const MEMO_MAX_AGE_H     = 12;   // re-run diligence if older
const SCENARIO_MAX_AGE_H = 24;   // re-run scenario lab if older

// ── CLI args ─────────────────────────────────────────────────────────────────
function getArg(flag, fallback = null) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const hoursArg    = parseFloat(getArg('--hours', '1'));
const tickersRaw  = getArg('--tickers', '');
const intervalArg = parseInt(getArg('--interval', '30'), 10);
const maxCostArg  = getArg('--max-cost') ? parseFloat(getArg('--max-cost')) : null;
const preScreen   = process.argv.includes('--pre-screen');

// ── Universe / ticker resolution ──────────────────────────────────────────────
function loadUniverse() {
  try { return JSON.parse(fs.readFileSync(UNIVERSE, 'utf8')); } catch { return { sectors: {} }; }
}

function resolveTickerList(raw) {
  if (!raw || raw === '') return [];
  const upper = raw.toUpperCase();
  if (upper === 'ALL') {
    const u = loadUniverse();
    return Object.values(u.sectors).flatMap(s => s.tickers || []);
  }
  const u = loadUniverse();
  for (const [name, data] of Object.entries(u.sectors)) {
    if (name.toUpperCase() === upper) return data.tickers || [];
  }
  return raw.split(',').map(t => t.trim().toUpperCase()).filter(Boolean);
}

let tickersArg = resolveTickerList(tickersRaw);
if (tickersArg.length === 0) {
  const u = loadUniverse();
  console.error('No tickers resolved. Use: --tickers AAPL,MSFT | ALL | SectorName');
  console.error('Available sectors:', Object.keys(u.sectors).join(', '));
  process.exit(1);
}

// ── Pre-screen filter ─────────────────────────────────────────────────────────
// When --pre-screen is set, intersect tickersArg with the latest screen-results.json.
// Tickers that didn't pass the quantitative screen are skipped for this run.
if (preScreen) {
  const SCREEN_RESULT = path.join(WORKDIR, 'output', 'screen-results.json');
  try {
    const res     = JSON.parse(fs.readFileSync(SCREEN_RESULT, 'utf8'));
    const passing = new Set((res.tickers || []).map(t => t.toUpperCase()));
    const before  = tickersArg.length;
    tickersArg    = tickersArg.filter(t => passing.has(t));
    const dropped = before - tickersArg.length;
    process.stderr.write(`[RUNNER] --pre-screen: ${tickersArg.length}/${before} tickers passed screen (${dropped} filtered out)\n`);
    if (tickersArg.length === 0) {
      process.stderr.write('[RUNNER] --pre-screen: no tickers survived the screen filter. Exiting.\n');
      process.exit(0);
    }
  } catch (err) {
    process.stderr.write(`[RUNNER] --pre-screen: could not load screen-results.json (${err.message}) — ignoring filter\n`);
  }
}

// ── Progress / logging ────────────────────────────────────────────────────────
function progress(event, data = {}) {
  process.stdout.write(`RUNNER_PROGRESS:${JSON.stringify({ event, ts: Date.now(), ...data })}\n`);
}
function log(msg) {
  process.stderr.write(`[${new Date().toISOString()}] [RUNNER] ${msg}\n`);
}

// ── File helpers ──────────────────────────────────────────────────────────────
function fileAgeHours(filePath) {
  try { return (Date.now() - fs.statSync(filePath).mtimeMs) / 3_600_000; }
  catch { return Infinity; }
}

function findLatestFile(dir, prefix, suffix = '.md') {
  try {
    const files = fs.readdirSync(dir)
      .filter(f => f.startsWith(prefix) && f.endsWith(suffix))
      .sort().reverse();
    return files.length ? path.join(dir, files[0]) : null;
  } catch { return null; }
}

function readVerdictFromMemo(memoPath) {
  try {
    const content = fs.readFileSync(memoPath, 'utf8');
    const m = content.match(/BOTJOHN_VERDICT:(PROCEED|REVIEW|KILL)/) ||
              content.match(/VERDICT:\s*(PROCEED|REVIEW|KILL)/i);
    return m ? m[1].toUpperCase() : 'REVIEW';
  } catch { return 'REVIEW'; }
}

// ── Verdict cache ─────────────────────────────────────────────────────────────
function loadVerdictCache() {
  try {
    const raw = JSON.parse(fs.readFileSync(VERDICT_CACHE, 'utf8'));
    return raw.tickers || {};
  } catch { return {}; }
}

function saveVerdictCache(tickers) {
  fs.writeFileSync(VERDICT_CACHE, JSON.stringify({
    _comment: 'Persistent verdict history. Written by pipeline-runner. Controls skip logic for repeat scans.',
    _schema:  '{ verdict, consecutiveKills, lastRun, skipUntil, lastSignal }',
    tickers,
  }, null, 2));
}

/**
 * Returns a skip reason string if the ticker should be bypassed this scan,
 * or null if it should proceed normally.
 */
function shouldSkipTicker(cache, ticker) {
  const entry = cache[ticker];
  if (!entry || !entry.skipUntil || Date.now() >= entry.skipUntil) return null;
  const until = new Date(entry.skipUntil).toUTCString();
  return `KILL×${entry.consecutiveKills} — skip until ${until}`;
}

/**
 * Update cache entry. Only update if ticker ran diligence this scan (not cache-skipped).
 */
function updateVerdictCache(cache, ticker, verdict, signal = null) {
  const prev = cache[ticker] || {};
  if (verdict === 'KILL') {
    const kills = (prev.consecutiveKills || 0) + 1;
    const skipHours = kills >= 3 ? 96 : kills === 2 ? 48 : 24;
    cache[ticker] = { verdict, consecutiveKills: kills, lastRun: Date.now(), skipUntil: Date.now() + skipHours * 3_600_000, lastSignal: signal };
  } else {
    cache[ticker] = { verdict, consecutiveKills: 0, lastRun: Date.now(), skipUntil: null, lastSignal: signal };
  }
}

// ── Generic parallel executor ─────────────────────────────────────────────────
/**
 * Run `fn` over `items` with at most `concurrency` tasks in flight at once.
 * Returns results array in original order.
 */
async function runConcurrent(items, fn, concurrency) {
  if (items.length === 0) return [];
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i, items.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return results;
}

// ── Child process spawners ────────────────────────────────────────────────────
const SPAWN_ENV = {
  HOME:         CLAUDE_HOME,
  USER:         'claudebot',
  LOGNAME:      'claudebot',
  CLAUDE_BIN,
  OPENCLAW_DIR: WORKDIR,
  SUDO_USER:    undefined,
  SUDO_UID:     undefined,
  SUDO_GID:     undefined,
  SUDO_COMMAND: undefined,
};

function spawnNode(scriptPath, args) {
  return new Promise((resolve, reject) => {
    let stdout = '', lineBuf = '', stderr = '';
    const proc = spawn('node', [scriptPath, ...args], {
      cwd: WORKDIR, uid: CLAUDE_UID, gid: CLAUDE_GID,
      env: { ...process.env, ...SPAWN_ENV },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    proc.stdout.on('data', (chunk) => {
      lineBuf += chunk.toString();
      const lines = lineBuf.split('\n');
      lineBuf = lines.pop();
      for (const line of lines) {
        if (line.startsWith('BOTJOHN_PROGRESS:') || line.startsWith('TRADE_PROGRESS:')) {
          process.stdout.write(line + '\n');
        } else {
          stdout += line + '\n';
        }
      }
    });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', (code) => {
      if (lineBuf) stdout += lineBuf;
      if (code === 0 || stdout.trim()) resolve(stdout.trim());
      else reject(new Error(stderr.trim().slice(0, 300) || `exit ${code}`));
    });
    proc.on('error', reject);
  });
}

function spawnScenario(ticker) {
  return new Promise((resolve, reject) => {
    const proc = spawn('bash', [path.join(WORKDIR, 'scripts', 'scenario.sh'), ticker], {
      cwd: WORKDIR, uid: CLAUDE_UID, gid: CLAUDE_GID,
      env: { ...process.env, ...SPAWN_ENV },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '', stderr = '';
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim().slice(0, 300) || `scenario.sh exit ${code}`));
    });
    proc.on('error', reject);
  });
}

// ── Phase 1: Diligence (one ticker) ──────────────────────────────────────────
async function runDiligence(ticker, idx, total, scanNum, verdictCache) {
  progress('TICKER_START', { ticker, scanNum, tickerIdx: idx + 1, totalTickers: total });

  // Verdict-cache KILL skip
  const skipReason = shouldSkipTicker(verdictCache, ticker);
  if (skipReason) {
    log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — CACHE SKIP: ${skipReason}`);
    progress('DILIGENCE_CACHE_SKIP', { ticker, scanNum, reason: skipReason });
    return { ticker, memoPath: null, verdict: 'KILL', cacheSkip: true };
  }

  // Check existing memo age
  const memoPath = findLatestFile(MEMOS_DIR, `${ticker}-diligence-`);
  const memoAge  = memoPath ? fileAgeHours(memoPath) : Infinity;

  if (memoAge <= MEMO_MAX_AGE_H) {
    const verdict = readVerdictFromMemo(memoPath);
    log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — diligence SKIPPED (${memoAge.toFixed(1)}h old) verdict=${verdict}`);
    progress('DILIGENCE_SKIPPED', { ticker, scanNum, ageHours: memoAge.toFixed(1), verdict, memoFile: path.basename(memoPath) });
    return { ticker, memoPath, verdict, cacheSkip: false };
  }

  // Budget check before spawning
  try {
    await budget.checkpoint(ticker + ':diligence', 'haiku');
  } catch (err) {
    progress('BUDGET_HALT_TICKER', { ticker, scanNum, reason: err.message });
    // Return with stale memo if any
    const verdict = memoPath ? readVerdictFromMemo(memoPath) : 'REVIEW';
    return { ticker, memoPath, verdict, budgetHalt: true };
  }

  log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — diligence START`);
  progress('DILIGENCE_START', { ticker, scanNum });

  try {
    await spawnNode(path.join(WORKDIR, 'scripts', 'orchestrator.js'), [ticker]);
    const freshMemo = findLatestFile(MEMOS_DIR, `${ticker}-diligence-`);
    const verdict   = freshMemo ? readVerdictFromMemo(freshMemo) : 'REVIEW';
    log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — diligence DONE verdict=${verdict}`);
    progress('DILIGENCE_COMPLETE', { ticker, scanNum, verdict, memoFile: freshMemo ? path.basename(freshMemo) : null });
    return { ticker, memoPath: freshMemo, verdict, cacheSkip: false };
  } catch (err) {
    log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — diligence ERROR: ${err.message}`);
    progress('DILIGENCE_ERROR', { ticker, scanNum, error: err.message });
    // Keep stale memo if any
    const verdict = memoPath ? readVerdictFromMemo(memoPath) : 'ERROR';
    return { ticker, memoPath, verdict: 'ERROR', error: err.message };
  }
}

// ── Phase 2: Scenario Lab (one ticker) ───────────────────────────────────────
async function runScenario(ticker, idx, total, scanNum) {
  const scenarioFile = findLatestFile(MEMOS_DIR, `${ticker}-scenario-comparison`);
  const scenarioAge  = scenarioFile ? fileAgeHours(scenarioFile) : Infinity;

  if (scenarioAge <= SCENARIO_MAX_AGE_H) {
    log(`[Scan ${scanNum}] ${ticker} — scenario SKIPPED (${scenarioAge.toFixed(1)}h old)`);
    progress('SCENARIO_SKIPPED', { ticker, scanNum, ageHours: scenarioAge.toFixed(1) });
    return { ticker, skipped: true };
  }

  try {
    await budget.checkpoint(ticker + ':scenario', 'sonnet');
  } catch (err) {
    progress('BUDGET_HALT_TICKER', { ticker, scanNum, reason: err.message });
    return { ticker, budgetHalt: true };
  }

  log(`[Scan ${scanNum}] [${idx + 1}/${total}] ${ticker} — scenario START`);
  progress('SCENARIO_START', { ticker, scanNum });

  try {
    await spawnScenario(ticker);
    log(`[Scan ${scanNum}] ${ticker} — scenario DONE`);
    progress('SCENARIO_COMPLETE', { ticker, scanNum });
    return { ticker, done: true };
  } catch (err) {
    log(`[Scan ${scanNum}] ${ticker} — scenario ERROR (non-fatal): ${err.message}`);
    progress('SCENARIO_ERROR', { ticker, scanNum, error: err.message });
    return { ticker, error: err.message };
  }
}

// ── Phase 3: Trade Pipeline (one ticker, sequential) ─────────────────────────
async function runTrade(ticker, memoPath, scanNum) {
  try {
    await budget.checkpoint(ticker + ':trade', 'sonnet');
  } catch (err) {
    progress('BUDGET_HALT_TICKER', { ticker, scanNum, reason: err.message });
    return { ticker, signal: null, budgetHalt: true };
  }

  log(`[Scan ${scanNum}] ${ticker} — trade START`);
  progress('TRADE_START', { ticker, scanNum });

  try {
    const out = await spawnNode(
      path.join(WORKDIR, 'scripts', 'trade-pipeline.js'),
      [ticker, '--memo', memoPath]
    );
    const m = out.match(/TRADE_VERDICT:(GO|WAIT|PASS|BLOCKED)/);
    const signal = m ? m[1] : 'UNKNOWN';
    log(`[Scan ${scanNum}] ${ticker} — trade DONE signal=${signal}`);
    progress('TRADE_COMPLETE', { ticker, scanNum, signal });
    if (signal === 'GO') progress('TICKER_SIGNAL_GO', { ticker, scanNum });
    return { ticker, signal };
  } catch (err) {
    log(`[Scan ${scanNum}] ${ticker} — trade ERROR: ${err.message}`);
    progress('TRADE_ERROR', { ticker, scanNum, error: err.message });
    return { ticker, signal: 'ERROR', error: err.message };
  }
}

// ── Main scan loop ────────────────────────────────────────────────────────────
async function main() {
  // Write PID into session state
  try {
    const raw = JSON.parse(fs.readFileSync(budget.STATE_FILE, 'utf8'));
    raw.runnerPid = process.pid;
    fs.writeFileSync(budget.STATE_FILE, JSON.stringify(raw, null, 2));
  } catch { /* standalone — no session state */ }

  log(`RUNNER START — tickers=[${tickersArg.join(',')}] hours=${hoursArg} interval=${intervalArg}m`);
  progress('RUNNER_START', {
    tickers: tickersArg, durationHours: hoursArg, intervalMin: intervalArg, maxCostUSD: maxCostArg,
  });

  const endTime    = Date.now() + hoursArg * 3_600_000;
  let   scanNum    = 0;
  const scanResults = [];

  while (Date.now() < endTime) {
    scanNum++;
    const scanStart = Date.now();
    const timeLeft  = endTime - scanStart;

    // Pre-scan budget check
    try {
      await budget.checkpoint('runner:scan', 'default');
    } catch (err) {
      log(`Budget halted before scan ${scanNum}: ${err.message}`);
      progress('RUNNER_HALTED', { reason: err.message, scanNum });
      break;
    }

    const h = Math.floor(timeLeft / 3_600_000);
    const m = Math.floor((timeLeft % 3_600_000) / 60_000);
    log(`SCAN ${scanNum} START — ${tickersArg.length} tickers | ${h}h ${m}m remaining`);
    progress('SCAN_START', { scanNum, tickers: tickersArg, timeRemainingMs: timeLeft });

    const verdictCache = loadVerdictCache();
    const tradeSignals = {};   // ticker → signal, populated in phase 3
    let   budgetHalted = false;

    // ── PHASE 1: Diligence (concurrency = 3) ────────────────────────────────
    log(`SCAN ${scanNum} — Phase 1: Diligence (×3 parallel, ${tickersArg.length} tickers)`);
    progress('PHASE_START', { scanNum, phase: 'diligence', concurrency: 3, tickers: tickersArg });

    const diligenceResults = await runConcurrent(
      tickersArg,
      (ticker, idx, total) => runDiligence(ticker, idx, total, scanNum, verdictCache),
      3
    );

    const verdictMap = {};
    for (const r of diligenceResults) verdictMap[r.ticker] = r.verdict;
    progress('PHASE_COMPLETE', { scanNum, phase: 'diligence', verdicts: verdictMap });

    // ── PHASE 2: Scenario Lab (PROCEED only, concurrency = 2) ───────────────
    const proceedTickers = diligenceResults
      .filter(r => r.verdict === 'PROCEED' && r.memoPath)
      .map(r => r.ticker);

    if (proceedTickers.length > 0) {
      log(`SCAN ${scanNum} — Phase 2: Scenario Lab (×2 parallel, ${proceedTickers.length} PROCEED tickers)`);
      progress('PHASE_START', { scanNum, phase: 'scenario', concurrency: 2, tickers: proceedTickers });
      await runConcurrent(
        proceedTickers,
        (ticker, idx, total) => runScenario(ticker, idx, total, scanNum),
        2
      );
      progress('PHASE_COMPLETE', { scanNum, phase: 'scenario' });
    } else {
      log(`SCAN ${scanNum} — Phase 2: Scenario skipped (no PROCEED tickers)`);
      progress('PHASE_SKIP', { scanNum, phase: 'scenario', reason: 'no PROCEED tickers' });
    }

    // ── PHASE 3: Trade Pipeline (non-KILL, PROCEED first, sequential) ────────
    // PROCEED tickers first — they have the most up-to-date scenario context
    const tradeCandidates = diligenceResults
      .filter(r => r.memoPath && r.verdict !== 'KILL' && r.verdict !== 'ERROR' && !r.cacheSkip)
      .sort((a, b) => ({ PROCEED: 0, REVIEW: 1 }[a.verdict] ?? 2) - ({ PROCEED: 0, REVIEW: 1 }[b.verdict] ?? 2));

    if (tradeCandidates.length > 0) {
      log(`SCAN ${scanNum} — Phase 3: Trade Pipeline (sequential, ${tradeCandidates.length} tickers — PROCEED first)`);
      progress('PHASE_START', { scanNum, phase: 'trade', concurrency: 1, tickers: tradeCandidates.map(r => r.ticker) });

      for (const { ticker, memoPath } of tradeCandidates) {
        const result = await runTrade(ticker, memoPath, scanNum);
        tradeSignals[ticker] = result.signal;
        if (result.budgetHalt) {
          budgetHalted = true;
          progress('SCAN_PARTIAL', { scanNum, reason: 'budget halt during trade phase' });
          break;
        }
      }

      progress('PHASE_COMPLETE', { scanNum, phase: 'trade' });
    } else {
      log(`SCAN ${scanNum} — Phase 3: No trade candidates`);
      progress('PHASE_SKIP', { scanNum, phase: 'trade', reason: 'no candidates' });
    }

    // ── Verdict cache update (all non-cache-skipped tickers) ─────────────────
    for (const r of diligenceResults) {
      if (!r.cacheSkip) {
        updateVerdictCache(verdictCache, r.ticker, r.verdict, tradeSignals[r.ticker] || null);
      }
    }
    saveVerdictCache(verdictCache);

    // ── Scan summary ─────────────────────────────────────────────────────────
    const scanElapsed = Date.now() - scanStart;
    const summary = diligenceResults.map(r => ({
      ticker:   r.ticker,
      verdict:  r.verdict,
      signal:   tradeSignals[r.ticker] || null,
      skipped:  r.cacheSkip || false,
    }));

    progress('SCAN_COMPLETE', { scanNum, elapsed: scanElapsed, summary });
    scanResults.push({ scanNum, summary });

    if (budgetHalted) break;

    // ── Wait for next scan ───────────────────────────────────────────────────
    const nextScanAt = scanStart + intervalArg * 60_000;
    const waitMs     = Math.min(nextScanAt - Date.now(), endTime - Date.now() - 10_000);

    if (waitMs > 30_000) {
      const wMin = Math.round(waitMs / 60_000);
      log(`SCAN ${scanNum} DONE — next in ${wMin}m`);
      progress('SCAN_WAITING', { scanNum, nextScanMs: waitMs, nextScanMin: wMin });
      await new Promise(r => setTimeout(r, waitMs));
    } else {
      break; // not enough time for another full scan
    }
  }

  log('RUNNER END');
  progress('RUNNER_COMPLETE', { scansCompleted: scanNum, allResults: scanResults });
  budget.endSession();
}

main().catch(err => {
  log(`FATAL: ${err.message}`);
  progress('RUNNER_ERROR', { error: err.message });
  console.error(err);
  process.exit(1);
});

process.on('SIGTERM', () => {
  log('SIGTERM — runner stopping');
  progress('RUNNER_HALTED', { reason: 'SIGTERM' });
  process.exit(0);
});
