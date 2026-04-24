'use strict';

/**
 * Subagent output verification contracts.
 * Prevents silent failures — every agent must prove it did useful work.
 */

const fs = require('fs');

const CONTRACTS = {
  research: (output) => {
    const text = typeof output === 'string' ? output : JSON.stringify(output);
    // Must contain at least one substantive findings block (>50 chars of content)
    const hasContent = text.length > 50;
    const hasSource  = /source[s]?\s*[:=]/i.test(text) || /\[(https?|SEC|FMP|Polygon)/i.test(text);
    if (!hasContent) return 'Output too short — no meaningful findings';
    if (!hasSource)  return 'No source attribution found in research output';
    return null;
  },

};

const UNVERIFIED_BANNER = `\n\n---\n⚠️ **UNVERIFIED**: One or more pipeline stages did not pass output verification. Review raw outputs before acting on this report.\n---\n`;

/**
 * Verify a subagent's output against its contract.
 * @param {string} agentName  Subagent type (e.g. 'research'). Agents without a contract pass-through as skipped.
 * @param {*}      output     The agent's output (string or object)
 * @param {Object} context    Optional — { ticker, reportPath, skill }
 * @returns {{ verified: boolean, agentName, failure: string|null }}
 */
async function verifySubagentOutput(agentName, output, context = {}) {
  const contract = CONTRACTS[agentName];
  if (!contract) {
    return { verified: true, agentName, failure: null, skipped: true };
  }

  const failure = contract(output, context);
  const verified = failure === null;

  // Update pipeline_runs status (best-effort)
  try {
    const { query } = require('../database/postgres');
    if (context.runId) {
      await query(
        `UPDATE pipeline_runs SET verification_status = $1 WHERE id = $2`,
        [verified ? 'VERIFIED' : 'UNVERIFIED', context.runId]
      );
    }
  } catch { /* non-blocking */ }

  if (!verified) {
    console.error(`[SECURITY_ALERT] verification: ${agentName} UNVERIFIED — ${failure}`);
    if (context.ticker) console.error(`  ticker: ${context.ticker}`);
    if (context.skill)  console.error(`  skill: ${context.skill}`);
  } else {
    console.log(`[verification] ${agentName} VERIFIED`);
  }

  return { verified, agentName, failure };
}

/**
 * Append unverified banner to a report file.
 */
function appendUnverifiedBanner(reportPath) {
  if (!fs.existsSync(reportPath)) return;
  fs.appendFileSync(reportPath, UNVERIFIED_BANNER);
}

/**
 * Verify all stages of a completed pipeline run.
 * Returns { allVerified, results: [{ agentName, verified, failure }] }
 */
async function verifyPipeline(stages, context = {}) {
  const results = [];
  let allVerified = true;

  for (const { agentName, output } of stages) {
    const r = await verifySubagentOutput(agentName, output, context);
    results.push(r);
    if (!r.verified && !r.skipped) allVerified = false;
  }

  return { allVerified, results };
}

module.exports = { verifySubagentOutput, verifyPipeline, appendUnverifiedBanner };
