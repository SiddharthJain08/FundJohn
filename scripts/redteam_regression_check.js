#!/usr/bin/env node
'use strict';

/**
 * redteam_regression_check.js — Task S1 calibration gate.
 *
 * Runs strategy_redteam.js's redteamStrategy() against the two fixtures in
 * tests/fixtures/redteam/ using the REAL claude-bin (no mocks). Exits 0 iff:
 *   - lookahead_fixture.py -> verdict='block' with >=1 CRITICAL finding whose
 *     concern/evidence mentions the future bar / shift(-1) bug, AND
 *   - clean_fixture.py     -> verdict='pass' with ZERO critical findings.
 * Prints both JSON results to stdout regardless of outcome.
 *
 * Usage: node scripts/redteam_regression_check.js
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '..');

// Load .env so CLAUDE_BIN / OPENCLAW_REDTEAM_MODEL overrides are present when
// run standalone (mirrors mastermind_code_review.js's CLI bootstrap).
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) { /* no .env — rely on process env */ }

const { redteamStrategy } = require('../src/agent/curators/strategy_redteam');

const LOOKAHEAD_FIXTURE = path.join(OPENCLAW_DIR, 'tests/fixtures/redteam/lookahead_fixture.py');
const CLEAN_FIXTURE     = path.join(OPENCLAW_DIR, 'tests/fixtures/redteam/clean_fixture.py');

const FUTURE_BAR_RE = /shift\(\s*-\d+\s*\)|future[\s_-]*bar|tomorrow|look-?ahead|lookahead/i;

function mentionsFutureBar(finding) {
  return FUTURE_BAR_RE.test(finding.concern || '') || FUTURE_BAR_RE.test(finding.evidence || '');
}

async function main() {
  console.error('[redteam-check] running lookahead_fixture.py through strategy_redteam...');
  const lookaheadResult = await redteamStrategy({ implPath: LOOKAHEAD_FIXTURE });
  console.log('lookahead_fixture.py ->');
  console.log(JSON.stringify(lookaheadResult, null, 2));

  console.error('[redteam-check] running clean_fixture.py through strategy_redteam...');
  const cleanResult = await redteamStrategy({ implPath: CLEAN_FIXTURE });
  console.log('clean_fixture.py ->');
  console.log(JSON.stringify(cleanResult, null, 2));

  const lookaheadCritical = lookaheadResult.findings.filter((f) => f.severity === 'critical');
  const lookaheadOk =
    lookaheadResult.verdict === 'block' &&
    lookaheadCritical.length >= 1 &&
    lookaheadCritical.some(mentionsFutureBar) &&
    !lookaheadResult.infra_fail;

  const cleanCritical = cleanResult.findings.filter((f) => f.severity === 'critical');
  const cleanOk =
    cleanResult.verdict === 'pass' &&
    cleanCritical.length === 0 &&
    !cleanResult.infra_fail;

  console.error(
    `[redteam-check] lookahead_fixture: ${lookaheadOk ? 'PASS' : 'FAIL'} ` +
    `(verdict=${lookaheadResult.verdict}, critical=${lookaheadCritical.length}, infra_fail=${lookaheadResult.infra_fail})`
  );
  console.error(
    `[redteam-check] clean_fixture: ${cleanOk ? 'PASS' : 'FAIL'} ` +
    `(verdict=${cleanResult.verdict}, critical=${cleanCritical.length}, infra_fail=${cleanResult.infra_fail})`
  );

  if (lookaheadOk && cleanOk) {
    console.error('[redteam-check] CALIBRATION GATE PASSED');
    process.exit(0);
  }
  console.error('[redteam-check] CALIBRATION GATE FAILED');
  process.exit(1);
}

main().catch((e) => {
  console.error('[redteam-check] FATAL:', e.message, e.stack);
  process.exit(1);
});
