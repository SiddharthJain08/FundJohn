'use strict';

/**
 * Subagent output verification contracts.
 * Prevents silent failures — every agent must prove it did useful work.
 */

const fs   = require('fs');
const path = require('path');

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

  'data-prep': (output, context) => {
    const text = typeof output === 'string' ? output : JSON.stringify(output);
    // Must report rows processed
    const rowsMatch = text.match(/rows?[_\s]*(?:processed|written|inserted|stored)[^\d]*(\d+)/i)
                   || text.match(/(\d+)\s*rows?\s*(processed|written|inserted)/i);
    if (!rowsMatch) return 'No rowsProcessed count found in data-prep output';
    const count = parseInt(rowsMatch[1]);
    if (count === 0) return 'rowsProcessed = 0 — no data was written';
    return null;
  },

  'equity-analyst': (output) => {
    const text = typeof output === 'string' ? output : JSON.stringify(output);
    const hasVerdict  = /\b(PASS|FAIL|REVIEW|PROCEED|KILL|BLOCKED|REDUCED)\b/.test(text);
    const hasRationale = text.length > 100;
    if (!hasVerdict)   return 'No verdict (PASS/FAIL/REVIEW/PROCEED/KILL) found in analyst output';
    if (!hasRationale) return 'Rationale too short (<100 chars) — insufficient analysis';
    return null;
  },

  compute: (output) => {
    const text = typeof output === 'string' ? output : JSON.stringify(output);
    // Must contain at least one finite numeric result
    const numericFields = [
      'positionSize', 'position_size', 'riskScore', 'risk_score',
      'dcfValue', 'dcf_value', 'kellyFraction', 'kelly_fraction',
      'targetPrice', 'target_price', 'ev_ntm', 'evNtm',
    ];
    const hasNumeric = numericFields.some(f => {
      const re = new RegExp(`["']?${f}["']?\\s*[=:]\\s*([\\d.]+)`, 'i');
      const m = text.match(re);
      return m && isFinite(parseFloat(m[1]));
    });
    // Also accept any pattern like "X: 12.3" or "X = 4.56"
    const hasAnyNumber = /:\s*\d+\.?\d*\b/.test(text) && !/:\s*0\b/.test(text.replace(/\d/g, '0'));
    if (!hasNumeric && !hasAnyNumber) return 'No finite numeric result found in compute output';
    return null;
  },

  'report-builder': (output, context) => {
    // Check the output file on disk
    if (context && context.reportPath) {
      if (!fs.existsSync(context.reportPath)) {
        return `Report file not found: ${context.reportPath}`;
      }
      const stat = fs.statSync(context.reportPath);
      if (stat.size < 500) return `Report file too small (${stat.size} bytes < 500)`;
      const content = fs.readFileSync(context.reportPath, 'utf8');
      const headers = (content.match(/^#{1,2} /gm) || []).length;
      if (headers < 3) return `Report has only ${headers} section header(s) — need at least 3`;
      return null;
    }
    // Fallback: check output text itself
    const text = typeof output === 'string' ? output : JSON.stringify(output);
    if (text.length < 500) return 'Report output too short (<500 chars)';
    const headers = (text.match(/^#{1,2} /gm) || []).length;
    if (headers < 3) return `Report has only ${headers} section header(s) — need at least 3`;
    return null;
  },
};

const UNVERIFIED_BANNER = `\n\n---\n⚠️ **UNVERIFIED**: One or more pipeline stages did not pass output verification. Review raw outputs before acting on this report.\n---\n`;

/**
 * Verify a subagent's output against its contract.
 * @param {string} agentName  One of: research, data-prep, equity-analyst, compute, report-builder
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
