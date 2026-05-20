'use strict';

/**
 * synthesizer.js — per-strategy Mastermind Opus synthesizer pass.
 *
 * Reads (original memo + 3 critiques + last-30d P&L + open positions +
 * last sizing recommendation), produces strategy_synthesis row with the
 * adjusted_recommended_size_pct. Falls back to original on any failure.
 */

const path             = require('node:path');
const fs               = require('node:fs');
const { spawn }        = require('node:child_process');
const { resolveModel } = require('../config/resolve_model.js');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const PROMPT_PATH = path.join(ROOT, 'src', 'agent', 'prompts', 'subagents', 'mastermind-synthesizer.md');

const SYNTH_BUDGET_USD = 0.50;
const SYNTH_TIMEOUT_MS = 180_000;

let _runnerOverride = null;
let _writerOverride = null;

function _setRunnerForTests(fn) { _runnerOverride = fn; }
function _setWriterForTests(fn) { _writerOverride = fn; }

function _buildPrompt(memo, critiques, trades, openPositions, originalSizePct, lastRec) {
  const template = fs.readFileSync(PROMPT_PATH, 'utf8');
  const payload = {
    original_memo:                memo.markdown_body,
    original_recommended_size_pct: originalSizePct,
    critiques:                    critiques.map(c => ({
      critic_role:   c.critic_role,
      critique_text: c.critique_text,
      cited_metrics: c.cited_metrics,
    })),
    last_30d_pnl:                 trades,
    current_open_positions:       openPositions,
    last_sizing_recommendation:   lastRec,
  };
  return template + '\n\n## INPUT\n```json\n' + JSON.stringify(payload, null, 2) + '\n```';
}

async function _defaultRunner(prompt) {
  const model = resolveModel('mastermind', 'synthesize', 'synthesizer');
  return new Promise((resolve, reject) => {
    const proc = spawn('/usr/local/bin/claude-bin', [
      '--print',
      '--output-format', 'json',
      '--model', model,
      '--max-budget-usd', SYNTH_BUDGET_USD.toFixed(2),
    ], { stdio: ['pipe', 'pipe', 'pipe'] });

    const timer = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`synthesizer timed out after ${SYNTH_TIMEOUT_MS}ms`));
    }, SYNTH_TIMEOUT_MS);

    let stdout = '', stderr = '';
    proc.stdout.on('data', (d) => stdout += d);
    proc.stderr.on('data', (d) => stderr += d);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return reject(new Error(`synthesizer exited ${code}: ${stderr.slice(0, 200)}`));
      }
      try {
        const env = JSON.parse(stdout);
        resolve(env.result || stdout);
      } catch {
        resolve(stdout);
      }
    });
    proc.stdin.end(prompt);
  });
}

async function _defaultWriter(row) {
  const { Pool } = require('pg');
  if (!_defaultWriter._pool) {
    _defaultWriter._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  await _defaultWriter._pool.query(
    `INSERT INTO strategy_synthesis
       (strategy_id, week_of, synthesizer_text,
        original_recommended_size_pct, adjusted_recommended_size_pct,
        adjustment_reason, critics_accepted, critics_rejected, cost_usd)
     VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9)
     ON CONFLICT (strategy_id, week_of) DO UPDATE SET
       synthesizer_text              = EXCLUDED.synthesizer_text,
       original_recommended_size_pct = EXCLUDED.original_recommended_size_pct,
       adjusted_recommended_size_pct = EXCLUDED.adjusted_recommended_size_pct,
       adjustment_reason             = EXCLUDED.adjustment_reason,
       critics_accepted              = EXCLUDED.critics_accepted,
       critics_rejected              = EXCLUDED.critics_rejected,
       cost_usd                      = EXCLUDED.cost_usd,
       generated_at                  = NOW()`,
    [row.strategy_id, row.week_of, row.synthesizer_text || '',
     row.original_recommended_size_pct, row.adjusted_recommended_size_pct,
     row.adjustment_reason,
     JSON.stringify(row.critics_accepted || []),
     JSON.stringify(row.critics_rejected || []),
     row.cost_usd || null]
  );
}

function _parseSynthesis(raw) {
  let body = raw.trim();
  const fenced = body.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenced) body = fenced[1];
  const m = body.match(/\{[\s\S]*\}/);
  if (m) body = m[0];
  return JSON.parse(body);
}

async function synthesize(memo, critiques, trades, openPositions, originalSizePct, { weekOf }) {
  const runner = _runnerOverride || _defaultRunner;
  const writer = _writerOverride || _defaultWriter;

  // No critiques → no-op, persist audit row
  if (!critiques || critiques.length === 0) {
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: '',
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: 'ALL_CRITICS_FAILED, defaulted to original',
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  const prompt = _buildPrompt(memo, critiques, trades, openPositions, originalSizePct, null);
  let raw;
  try {
    raw = await runner(prompt);
  } catch (e) {
    console.warn(`[synthesizer] ${memo.strategy_id}: runner failed: ${e.message}`);
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: '',
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: `SYNTHESIZER_FAILED: ${e.message}`,
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  let parsed;
  try {
    parsed = _parseSynthesis(raw);
  } catch (e) {
    console.warn(`[synthesizer] ${memo.strategy_id}: parse failed: ${e.message}`);
    const row = {
      strategy_id: memo.strategy_id,
      week_of:     weekOf,
      synthesizer_text: raw.slice(0, 4000),
      original_recommended_size_pct: originalSizePct,
      adjusted_recommended_size_pct: originalSizePct,
      adjustment_reason: `SYNTHESIZER_PARSE_FAILED: ${e.message}`,
      critics_accepted: [],
      critics_rejected: [],
    };
    await writer(row);
    return row;
  }

  const row = {
    strategy_id: memo.strategy_id,
    week_of:     weekOf,
    synthesizer_text: raw.slice(0, 4000),
    original_recommended_size_pct: parsed.original_recommended_size_pct ?? originalSizePct,
    adjusted_recommended_size_pct: parsed.adjusted_recommended_size_pct ?? originalSizePct,
    adjustment_reason: parsed.adjustment_reason || '',
    critics_accepted:  parsed.critics_accepted || [],
    critics_rejected:  parsed.critics_rejected || [],
  };
  await writer(row);
  return row;
}

module.exports = { synthesize, _setRunnerForTests, _setWriterForTests };
