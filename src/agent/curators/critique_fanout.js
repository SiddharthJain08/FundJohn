'use strict';

/**
 * critique_fanout.js — for each eligible strategy, invoke 3 Sonnet critics
 * in parallel against the memo's recommendations, persist results to
 * strategy_memo_critiques.
 *
 * Per-critic failure → log + skip that row. All-3-fail → return summary
 * so the synthesizer can short-circuit to "no critics, default to original".
 */

const path             = require('node:path');
const fs               = require('node:fs');
const { spawn }        = require('node:child_process');
const { resolveModel } = require('../config/resolve_model.js');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const PROMPT_DIR = path.join(ROOT, 'src', 'agent', 'prompts', 'critics');

const CRITIC_ROLES = ['aggressive', 'conservative', 'neutral'];
const CRITIC_BUDGET_USD = 0.10;
const CRITIC_TIMEOUT_MS = 90_000;

// ── Overridable dependencies for tests ───────────────────────────────────

let _runnerOverride = null;
let _writerOverride = null;

function _setRunnerForTests(fn) { _runnerOverride = fn; }
function _setWriterForTests(fn) { _writerOverride = fn; }

// ── Default implementations ──────────────────────────────────────────────

function _loadPrompt(criticRole) {
  return fs.readFileSync(path.join(PROMPT_DIR, `${criticRole}_critic.md`), 'utf8');
}

function _buildPrompt(criticRole, memo, trades, openPositions) {
  const template = _loadPrompt(criticRole);
  const payload = {
    original_memo:         memo.markdown_body,
    original_recommendation: memo.recommendations,
    last_30d_pnl:          trades,
    current_open_positions: openPositions,
  };
  return template + '\n\n## INPUT\n```json\n' + JSON.stringify(payload, null, 2) + '\n```';
}

async function _defaultRunner(criticRole, prompt) {
  const model = resolveModel('mastermind', 'critique', `${criticRole}_critic`);
  return new Promise((resolve, reject) => {
    const proc = spawn('/usr/local/bin/claude-bin', [
      '--print',
      '--output-format', 'json',
      '--model', model,
      '--max-budget-usd', CRITIC_BUDGET_USD.toFixed(2),
    ], { stdio: ['pipe', 'pipe', 'pipe'] });

    const timer = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`${criticRole} timed out after ${CRITIC_TIMEOUT_MS}ms`));
    }, CRITIC_TIMEOUT_MS);

    let stdout = '', stderr = '';
    proc.stdout.on('data', (d) => stdout += d);
    proc.stderr.on('data', (d) => stderr += d);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return reject(new Error(`${criticRole} exited ${code}: ${stderr.slice(0, 200)}`));
      }
      try {
        const envelope = JSON.parse(stdout);
        resolve(envelope.result || stdout);
      } catch {
        resolve(stdout);  // raw — caller parses
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
    `INSERT INTO strategy_memo_critiques
       (strategy_id, week_of, critic_role, critique_text, cited_metrics, cost_usd, duration_sec)
     VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
     ON CONFLICT (strategy_id, week_of, critic_role) DO UPDATE SET
       critique_text = EXCLUDED.critique_text,
       cited_metrics = EXCLUDED.cited_metrics,
       cost_usd      = EXCLUDED.cost_usd,
       duration_sec  = EXCLUDED.duration_sec,
       generated_at  = NOW()`,
    [row.strategy_id, row.week_of, row.critic_role, row.critique_text,
     JSON.stringify(row.cited_metrics || {}), row.cost_usd || null, row.duration_sec || null]
  );
}

function _parseCritique(raw) {
  // Critic emits strict JSON; tolerate fence wrapping
  let body = raw.trim();
  const fenced = body.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenced) body = fenced[1];
  const m = body.match(/\{[\s\S]*\}/);
  if (m) body = m[0];
  return JSON.parse(body);
}

/**
 * Run all 3 critics in parallel for one strategy memo.
 * Returns { success_count, failure_count, persisted_roles }.
 */
async function runOne(memo, trades, openPositions, { weekOf }) {
  const runner = _runnerOverride || _defaultRunner;
  const writer = _writerOverride || _defaultWriter;
  const start  = Date.now();

  const results = await Promise.allSettled(
    CRITIC_ROLES.map(async (role) => {
      const t0 = Date.now();
      const prompt = _buildPrompt(role, memo, trades, openPositions);
      const raw    = await runner(role, prompt);
      const parsed = _parseCritique(raw);
      const duration = (Date.now() - t0) / 1000;
      await writer({
        strategy_id:   memo.strategy_id,
        week_of:       weekOf,
        critic_role:   role,
        critique_text: parsed.critique_text || raw.slice(0, 4000),
        cited_metrics: parsed.cited_metrics || {},
        cost_usd:      null,  // claude-bin envelope sometimes exposes this; not required
        duration_sec:  duration,
      });
      return role;
    })
  );

  const persisted_roles = results.filter(r => r.status === 'fulfilled').map(r => r.value);
  const failure_count   = results.filter(r => r.status === 'rejected').length;
  for (const r of results) {
    if (r.status === 'rejected') {
      console.warn(`[critique_fanout] ${memo.strategy_id}: critic failed: ${r.reason.message}`);
    }
  }
  return {
    success_count: persisted_roles.length,
    failure_count,
    persisted_roles,
    duration_sec: (Date.now() - start) / 1000,
  };
}

module.exports = { runOne, _setRunnerForTests, _setWriterForTests, CRITIC_ROLES };
