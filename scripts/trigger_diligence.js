#!/usr/bin/env node
/**
 * Trigger full diligence pipeline on one or more tickers.
 * Usage: node scripts/trigger_diligence.js NVDA GOOGL META
 *
 * Flow per ticker:
 *   1. python3 run_tier_b.py TICKER  — creates all data files
 *   2. swarm.runDiligence()          — research → compute → equity-analyst → report-builder
 *
 * Reports are written to workspaces/default/results/{TICKER}-{DATE}-memo.md
 * Verdict cache written to .agents/verdict-cache/{TICKER}-{DATE}.json
 */

'use strict';

require('dotenv').config({ path: `${__dirname}/../.env` });

const { execFileSync, spawnSync } = require('child_process');
const path = require('path');
const fs   = require('fs');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
process.chdir(OPENCLAW_DIR);

// Sync credentials to claudebot before spawning any subagents
function syncCredentials() {
  const src  = '/root/.claude/.credentials.json';
  const dest = '/home/claudebot/.claude/.credentials.json';
  try {
    if (fs.existsSync(src)) {
      fs.mkdirSync('/home/claudebot/.claude', { recursive: true });
      fs.copyFileSync(src, dest);
      fs.chownSync(dest, 1001, 1001);
      console.log('[trigger] Credentials synced to claudebot');
    }
  } catch (e) {
    console.warn('[trigger] Credential sync failed:', e.message);
  }
}

function runTierB(ticker) {
  console.log(`\n[trigger] Running TIER_B data prep for ${ticker}...`);
  const result = spawnSync('python3', ['scripts/run_tier_b.py', ticker], {
    cwd: OPENCLAW_DIR,
    stdio: 'inherit',
    env: { ...process.env },
    timeout: 240_000,
  });
  if (result.status !== 0) {
    throw new Error(`TIER_B data prep failed for ${ticker} (exit ${result.status})`);
  }
}

async function runAnalysis(ticker, workspace, swarm) {
  const threadId = `trigger-${ticker}-${Date.now()}`;

  const notify = (msg) => {
    const ts = new Date().toLocaleTimeString();
    console.log(`  [${ts}] ${msg}`);
  };

  console.log(`\n[trigger] Starting analysis pipeline for ${ticker}...`);

  // Run research → compute → equity-analyst → report-builder
  // (data-prep already done via run_tier_b.py, so call runTradePipeline directly)
  const taskDir = path.join(workspace, 'work', `${ticker}-diligence`);

  // Validate manifest before starting pipeline
  const manifestPath = path.join(taskDir, 'data', 'DATA_MANIFEST.json');
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`DATA_MANIFEST.json missing for ${ticker} — run TIER_B first`);
  }

  // Run the full pipeline: research first, then trade pipeline stages
  // Override: run research + trade pipeline together but with data already present
  const researchResult = await swarm.init({
    type: 'research', ticker, workspace, threadId, notify,
  });

  // Fulfill any data requests from research
  const dataRequester = require('../src/agent/data-requester');
  await dataRequester.fulfill(researchResult.output, taskDir, notify).catch(() => {});

  // Compute + equity-analyst in parallel
  const [computeResult, analystResult] = await Promise.all([
    swarm.init({ type: 'compute',        ticker, workspace, threadId, notify }),
    new Promise(r => setTimeout(r, 2000)).then(() =>
      swarm.init({ type: 'equity-analyst', ticker, workspace, threadId, notify, useBatch: false })
    ),
  ]);

  // Report-builder last
  const reportResult = await swarm.init({
    type: 'report-builder', ticker, workspace, threadId, notify,
  });

  return { researchResult, computeResult, analystResult, reportResult };
}

async function main() {
  const tickers = process.argv.slice(2).map(t => t.toUpperCase());
  if (tickers.length === 0) {
    // Default: top 5 from candidate queue
    const queuePath = '.agents/market-state/candidate_queue.json';
    if (!fs.existsSync(queuePath)) {
      console.error('No tickers specified and no candidate queue found. Run MARKET_STATE first.');
      process.exit(1);
    }
    const queue = JSON.parse(fs.readFileSync(queuePath, 'utf8'));
    tickers.push(...queue.slice(0, 5).map(c => c.ticker));
    console.log(`[trigger] No tickers specified — using top 5 from candidate queue: ${tickers.join(', ')}`);
  }

  syncCredentials();

  const swarm         = require('../src/agent/subagents/swarm');
  // Use the default workspace
  const workspaceMgr  = require('../src/workspace/manager');
  const workspace     = await workspaceMgr.getOrCreate('default');

  console.log(`\n${'='.repeat(60)}`);
  console.log(`OpenClaw Diligence Pipeline`);
  console.log(`Tickers: ${tickers.join(', ')}`);
  console.log(`Workspace: ${workspace}`);
  console.log(`${'='.repeat(60)}\n`);

  const results = [];

  for (const ticker of tickers) {
    const start = Date.now();
    try {
      // Step 1: Data prep
      runTierB(ticker);

      // Step 2: Analysis pipeline
      const analysis = await runAnalysis(ticker, workspace, swarm);
      const duration = Math.round((Date.now() - start) / 1000);

      console.log(`\n✅ ${ticker} complete in ${duration}s`);
      console.log(`   Research: ${analysis.researchResult.verification?.verified ? '✓' : '⚠'}`);
      console.log(`   Compute:  ${analysis.computeResult.verification?.verified ? '✓' : '⚠'}`);
      console.log(`   Analyst:  ${analysis.analystResult.verification?.verified ? '✓' : '⚠'}`);
      console.log(`   Report:   ${analysis.reportResult.verification?.verified ? '✓' : '⚠'}`);

      results.push({ ticker, status: 'COMPLETE', duration });

    } catch (err) {
      const duration = Math.round((Date.now() - start) / 1000);
      console.error(`\n❌ ${ticker} FAILED after ${duration}s: ${err.message}`);
      results.push({ ticker, status: 'FAILED', error: err.message, duration });
    }
  }

  console.log(`\n${'='.repeat(60)}`);
  console.log('Pipeline Summary');
  console.log(`${'='.repeat(60)}`);
  for (const r of results) {
    const icon = r.status === 'COMPLETE' ? '✅' : '❌';
    console.log(`${icon} ${r.ticker.padEnd(8)} ${r.status.padEnd(10)} ${r.duration}s${r.error ? ' — ' + r.error : ''}`);
  }

  const failed = results.filter(r => r.status === 'FAILED').length;
  process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => {
  console.error('[trigger] Fatal:', err);
  process.exit(1);
});
