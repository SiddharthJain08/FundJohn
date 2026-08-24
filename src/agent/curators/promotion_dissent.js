#!/usr/bin/env node
'use strict';

/**
 * promotion_dissent.js — Task S3: structured adversarial dissent at
 * candidate->live promotion.
 *
 * NON-VETO BY DESIGN: this runs strictly AFTER auto_approval.js's sweep has
 * already confirmed a candidate->live transition succeeded (the non-422
 * path). The promotion outcome is final before this module is ever called.
 * Any failure here — LLM error, malformed JSON, DB error, webhook error —
 * is logged and swallowed; it must never propagate back into the sweep in
 * a way that could look like (or become) a veto.
 *
 * Invocation idiom reused from strategy_redteam.js (sibling task S1):
 * claude-bin spawned via strategy_redteam.js's runOneShotDeprivileged()
 * (stdin prompt, stream-json output, bypassPermissions, read-only
 * disallowedTools) + _opus_oneshot.js's parseJsonBlock() for extraction.
 * Output is STRICT JSON, hand-validated by shape/enum (no new deps), one
 * retry on parse failure, then infra_fail=true recorded with an empty
 * dissent array — the same fail-open posture strategy_redteam.js uses,
 * adapted for a module that has no verdict to derive (a dissent is
 * advisory prose, not a pass/block decision).
 *
 * Root-privilege fix (2026-08-24 review, finding #1): this CLI's documented
 * usage on this box runs as root (the operator account; johnbot.service has
 * no User= directive either), and claude-bin refuses
 * `--permission-mode bypassPermissions` under root/sudo. Left on the plain
 * `runOneShot()` from _opus_oneshot.js, every invocation as root would
 * silently degrade to infra_fail=true with an EMPTY dissent and exit 0 —
 * indistinguishable from success on the command line. This module now uses
 * strategy_redteam.js's `runOneShotDeprivileged()` (already exported from
 * that file), which de-privileges the child to the `claudebot` service
 * account (uid/gid/HOME/CLAUDE_HOME) when `process.getuid() === 0`, the same
 * fix strategy_redteam.js shipped today for the identical gap. The CLI path
 * also now exits non-zero and WARNs on stderr when infra_fail is true (see
 * bottom of file) so an empty advisory row can never look like success.
 *
 * Discord post idiom reused from weekly_live_sharpe.js / position_recommender.js:
 * _discord_webhook.js's postToChannel('botjohn', 'botjohn-log', ...) — looks
 * up the webhook URL in agent_registry.webhook_urls and POSTs with the
 * Cloudflare-dodging User-Agent already baked into that helper. Never
 * hand-rolled here.
 *
 * CLI (manual/backfill runs):
 *   node src/agent/curators/promotion_dissent.js <strategy_id>
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const IMPL_DIR       = path.join(OPENCLAW_DIR, 'src/strategies/implementations');
const WORKSPACE      = path.join(OPENCLAW_DIR, 'workspaces/default');
const REGIME_FILE    = path.join(OPENCLAW_DIR, '.agents/market-state/regime_latest.json');

// .env so POSTGRES_URI etc. are populated when run standalone (mirrors
// auto_approval.js's / redteam_regression_check.js's bootstrap — never
// clobbers an already-set var, e.g. from a systemd EnvironmentFile or an
// explicit `env` wrapper when invoked as the claudebot user, which cannot
// read this repo's root-owned .env directly).
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) { /* no .env readable — rely on process env (e.g. EnvironmentFile) */ }

const { parseJsonBlock } = require('./_opus_oneshot');
const { runOneShotDeprivileged } = require('./strategy_redteam');
const { postToChannel } = require('./_discord_webhook');
const { MODELS } = require('../config/models');

// Sonnet 5 by default (same tier strategy_redteam.js uses for high-volume
// gates) — this fires once per successful promotion, not per candidate, but
// there is no reason to default to the heavier 1M Opus tier for a five-item
// structured critique. Override via OPENCLAW_DISSENT_MODEL.
const DEFAULT_MODEL = process.env.OPENCLAW_DISSENT_MODEL || MODELS.primary.model;
const TIMEOUT_MS    = parseInt(process.env.OPENCLAW_DISSENT_TIMEOUT_MS || '180000', 10) || 180_000;

const VALID_SEVERITIES = new Set(['note', 'material', 'severe']);
const SEVERITY_RANK    = { severe: 3, material: 2, note: 1 };
const CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

const RETRY_REMINDER = [
  '',
  'Your previous reply could not be parsed as the required JSON.',
  'STRICT JSON ONLY. Reply with EXACTLY one JSON object and NOTHING else —',
  'no markdown fences, no prose, no leading/trailing text:',
  '{"dissent": [{"concern": "...", "severity": "note"|"material"|"severe", "evidence": "..."}]}',
  '"dissent" must have between 1 and 5 items.',
].join('\n');

// ── DB ───────────────────────────────────────────────────────────────────
function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 2 });
  return _query._pool.query(sql, params);
}

// ── Manifest / impl resolution — same idiom as mastermind_code_review.js ──
function _loadManifest() {
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
}

function resolveImplPath(strategyId, manifest) {
  const entry = (manifest.strategies || {})[strategyId] || {};
  const canonical = entry.metadata && entry.metadata.canonical_file;
  const tries = [];
  if (canonical) tries.push(canonical);
  tries.push(`${strategyId}.py`, `${strategyId.toLowerCase()}.py`);
  for (const c of tries) {
    const p = path.join(IMPL_DIR, c);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

// ── Context loading (best-effort, defensive) ───────────────────────────────

/**
 * Latest primary-window backtest run + its per-regime sleeves. SELECT
 * DEFENSIVELY on sortino/cvar_5/tail_sortino — migration 148 landed the same
 * day as this task and a not-yet-migrated environment must still get a
 * (smaller) context rather than a hard failure.
 */
async function _latestPrimaryRun(query, sid) {
  const { rows } = await query(
    `SELECT run_id::text AS run_id, total_sharpe, total_max_dd_pct, total_trades,
            total_return_pct, total_sortino, total_calmar, window_kind,
            start_date, end_date, oos_days
       FROM strategy_backtest_runs
      WHERE strategy_id = $1 AND primary_window = TRUE
      ORDER BY run_at DESC LIMIT 1`, [sid]);
  return rows[0] || null;
}

async function _regimeSleeves(query, runId) {
  if (!runId) return [];
  try {
    const { rows } = await query(
      `SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar,
              sortino, cvar_5, tail_sortino, oos_days_in_regime
         FROM strategy_backtest_regimes WHERE run_id = $1 ORDER BY regime_state`,
      [runId]);
    return rows;
  } catch (_) {
    // Pre-148 environment: sortino/cvar_5/tail_sortino don't exist yet.
    const { rows } = await query(
      `SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar,
              oos_days_in_regime
         FROM strategy_backtest_regimes WHERE run_id = $1 ORDER BY regime_state`,
      [runId]);
    return rows;
  }
}

/** Top-5 similarity neighbors from the current-regime similarity matrix.
 * Best-effort: returns { neighbors: [], note } — never throws. */
async function _similarityNeighbors(query, sid, liveIds) {
  let regime = 'TRANSITIONING';
  try {
    const j = JSON.parse(fs.readFileSync(REGIME_FILE, 'utf8'));
    if (j && j.state) regime = j.state;
  } catch (_) { /* fall back to TRANSITIONING */ }
  try {
    const { rows } = await query(
      `SELECT matrix FROM strategy_similarity_matrix
        WHERE regime_state = $1 AND is_current
        ORDER BY computed_at DESC LIMIT 1`, [regime]);
    const matrix = rows[0] && rows[0].matrix;
    const row = matrix && matrix[sid];
    if (!row) {
      return { neighbors: [], note: `no current strategy_similarity_matrix row for regime ${regime} (or ${sid} absent from it)` };
    }
    const neighbors = Object.entries(row)
      .filter(([nid]) => nid !== sid)
      .map(([nid, rho]) => ({ strategy_id: nid, rho: Number(rho), live: liveIds.has(nid) }))
      .filter((n) => Number.isFinite(n.rho))
      .sort((a, b) => Math.abs(b.rho) - Math.abs(a.rho))
      .slice(0, 5);
    return { neighbors, note: neighbors.length ? null : `similarity row for regime ${regime} has no other strategies` };
  } catch (e) {
    return { neighbors: [], note: `similarity lookup failed: ${e.message}` };
  }
}

async function loadContext({ strategyId, query }) {
  const manifest = _loadManifest();
  const entry = (manifest.strategies || {})[strategyId] || null;
  const instrumentClass = (entry && entry.instrument_class) || 'equity';
  const qualifyingRegimes = (entry && entry.metadata && Array.isArray(entry.metadata.eligible_regimes))
    ? entry.metadata.eligible_regimes : [];

  let run = null, runError = null;
  try { run = await _latestPrimaryRun(query, strategyId); }
  catch (e) { runError = e.message; }

  let sleeves = [], sleevesError = null;
  if (run) {
    try { sleeves = await _regimeSleeves(query, run.run_id); }
    catch (e) { sleevesError = e.message; }
  }

  const liveIds = new Set(
    Object.keys(manifest.strategies || {})
      .filter((sid) => sid !== strategyId && manifest.strategies[sid].state === 'live'));

  const { neighbors, note: similarityNote } = await _similarityNeighbors(query, strategyId, liveIds);

  const implPath = resolveImplPath(strategyId, manifest);
  let code = null, codeError = null;
  if (implPath) {
    try { code = fs.readFileSync(implPath, 'utf8'); }
    catch (e) { codeError = e.message; }
  } else {
    codeError = 'implementation file not found via manifest.metadata.canonical_file / <id>.py fallback';
  }

  return {
    entry, instrumentClass, qualifyingRegimes,
    run, runError, sleeves, sleevesError,
    liveCount: liveIds.size, neighbors, similarityNote,
    implPath, code, codeError,
  };
}

// ── Prompt ──────────────────────────────────────────────────────────────

function _fmtSleeve(row) {
  const bits = [
    `sharpe=${row.sharpe}`, `trades=${row.trade_count}`, `max_dd_pct=${row.max_dd_pct}`,
    `calmar=${row.calmar}`, `oos_days=${row.oos_days_in_regime}`,
  ];
  if (row.sortino != null) bits.push(`sortino(annualized,equity-curve)=${row.sortino}`);
  if (row.cvar_5 != null) bits.push(`cvar_5(per-trade)=${row.cvar_5}`);
  if (row.tail_sortino != null) bits.push(`tail_sortino(per-trade)=${row.tail_sortino}`);
  return `  ${row.regime_state}: ${bits.join(', ')}`;
}

function buildPrompt({ strategyId, context }) {
  const { entry, instrumentClass, qualifyingRegimes, run, runError, sleeves,
          sleevesError, liveCount, neighbors, similarityNote, implPath, code, codeError } = context;
  const meta = entry && entry.metadata ? entry.metadata : {};
  const hypothesis = meta.description || meta.hypothesis || '(no description in manifest)';

  const runBlock = run
    ? [
        `total_sharpe=${run.total_sharpe}, total_max_dd_pct=${run.total_max_dd_pct}, `
          + `total_trades=${run.total_trades}, total_return_pct=${run.total_return_pct}`,
        run.total_sortino != null ? `total_sortino(annualized,equity-curve)=${run.total_sortino}` : null,
        run.total_calmar != null ? `total_calmar=${run.total_calmar}` : null,
        `window=${run.window_kind} ${run.start_date}..${run.end_date} (${run.oos_days} OOS days)`,
      ].filter(Boolean).join('\n')
    : `(no primary-window backtest on record${runError ? ` — query error: ${runError}` : ''})`;

  const sleeveBlock = sleeves.length
    ? sleeves.map(_fmtSleeve).join('\n')
    : `(no per-regime sleeves recorded${sleevesError ? ` — query error: ${sleevesError}` : ''})`;

  const neighborBlock = neighbors.length
    ? neighbors.map((n) => `  ${n.strategy_id}: rho=${n.rho.toFixed(3)}${n.live ? ' [LIVE]' : ''}`).join('\n')
    : `(${similarityNote || 'no similarity data available'})`;

  const codeBlock = code
    ? ['```python', code, '```'].join('\n')
    : `(implementation source unavailable: ${codeError})`;

  return [
    `You are an adversarial reviewer recording a structured DISSENT on a`,
    `quant strategy that has JUST been promoted candidate->live. This is`,
    `advisory only — the promotion is already final and cannot be reversed`,
    `by anything you say. Your job is to surface exactly what a numeric`,
    `Sharpe/drawdown/trade-count gate CANNOT see, for a human to review later.`,
    ``,
    `Strategy id: ${strategyId}`,
    `Instrument class: ${instrumentClass}`,
    `Hypothesis / description: ${hypothesis}`,
    `Promoted (qualifying) regimes: ${qualifyingRegimes.length ? qualifyingRegimes.join(', ') : '(none recorded)'}`,
    ``,
    `Latest primary-window backtest:`,
    runBlock,
    ``,
    `Per-regime sleeves (the gate judges each of these independently — a`,
    `regime with few OOS days can "qualify" on a small, possibly lucky,`,
    `sample; note oos_days_in_regime and trade counts explicitly for that):`,
    sleeveBlock,
    `NOTE on the two Sortino-shaped columns above, when present: "sortino" is`,
    `an ANNUALIZED Sortino from the equal-weight DAILY PORTFOLIO EQUITY CURVE`,
    `(pre-existing column); "tail_sortino" is a PER-TRADE Sortino (population`,
    `downside-deviation of per-trade pnl_pct) computed separately — they are`,
    `NOT the same metric. "cvar_5" is the mean of the worst 5% of per-trade`,
    `pnl_pct observations in that regime sleeve. Cite whichever are present`,
    `when discussing tail risk; do not conflate them.`,
    ``,
    `Top-5 similarity neighbors (current-regime strategy x strategy matrix;`,
    `[LIVE] = also currently live — a proxy for crowding/correlation against`,
    `the incumbent book, which today has ${liveCount} other live strategies):`,
    neighborBlock,
    ``,
    `Implementation${implPath ? ` (${path.basename(implPath)})` : ''}:`,
    codeBlock,
    ``,
    `Write a structured dissent covering ONLY what the numeric gate cannot`,
    `see. Consider, where the evidence supports it:`,
    `(a) REGIME-WINDOW UNDERSAMPLING — e.g. "qualifies in CRISIS on a window`,
    `    containing only N CRISIS days" — a thin or short-lived regime`,
    `    sample can pass the gate on a few lucky trades.`,
    `(b) CORRELATION / CROWDING with the incumbent live book — does this`,
    `    strategy duplicate exposure already held by highly-similar live`,
    `    strategies (see the neighbor list above)?`,
    `(c) ECONOMIC-MECHANISM PLAUSIBILITY vs curve-fit — does the hypothesis`,
    `    describe a mechanism that should persist out-of-sample, or does the`,
    `    code/parameterization look tuned to this specific backtest window?`,
    `(d) COST/CAPACITY ASSUMPTIONS AT LARGER SIZE — would slippage/impact`,
    `    plausibly erode the edge at a materially larger allocation than the`,
    `    backtest implies?`,
    `(e) TAIL PROFILE — cite sortino/cvar_5/tail_sortino when present; does`,
    `    the tail shape look worse than the headline Sharpe/DD suggest?`,
    ``,
    `Respond with STRICT JSON ONLY — no markdown code fences, no prose before`,
    `or after, exactly this shape and nothing else:`,
    `{"dissent": [{"concern": "<short label>", "severity": "note"|"material"|"severe", "evidence": "<specific, cite numbers/lines where possible>"}]}`,
    ``,
    `Between 1 and 5 items. Use "severe" only for something that, if true,`,
    `should prompt an operator to reconsider this promotion soon; "material"`,
    `for a real but non-urgent concern; "note" for a minor observation. If`,
    `you genuinely find nothing beyond minor notes, still return at least`,
    `one "note"-severity item — an empty dissent is not a valid response.`,
  ].join('\n');
}

/** Validate + normalize the parsed JSON by hand. Returns null if unusable. */
function validateDissentJson(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  if (!Array.isArray(obj.dissent)) return null;
  if (obj.dissent.length < 1 || obj.dissent.length > 5) return null;
  const dissent = [];
  for (const d of obj.dissent) {
    if (!d || typeof d !== 'object' || Array.isArray(d)) return null;
    if (typeof d.concern !== 'string' || !d.concern.trim()) return null;
    if (!VALID_SEVERITIES.has(d.severity)) return null;
    if (typeof d.evidence !== 'string' || !d.evidence.trim()) return null;
    dissent.push({ concern: d.concern.trim(), severity: d.severity, evidence: d.evidence.trim() });
  }
  return dissent;
}

/**
 * Run one claude-bin turn and try to extract a valid dissent array.
 * Returns { parsed, res } — parsed is null on any failure.
 */
async function _attempt(prompt, { model, runOneShotFn }) {
  const res = await runOneShotFn({
    prompt,
    model,
    cwd: WORKSPACE,
    disallowedTools: ['Bash', 'Write', 'Edit', 'NotebookEdit'],  // read-only audit
    timeoutMs: TIMEOUT_MS,
  });
  if (res.error) return { parsed: null, res };
  const parsed = validateDissentJson(parseJsonBlock(res.text));
  return { parsed, res };
}

function _severitySort(dissent) {
  return dissent.slice().sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity]);
}

/** `[dissent] <sid> → live (<regimes>): <top severity>: <top concern>` +
 * one line per remaining item. <=10 lines total (max 5 items -> 5 lines). */
function formatSummary({ strategyId, context, dissent, infraFail }) {
  const regimes = (context.qualifyingRegimes && context.qualifyingRegimes.length)
    ? context.qualifyingRegimes.join(', ') : '?';
  if (infraFail || dissent.length === 0) {
    return `[dissent] ${strategyId} → live (${regimes}): dissent unavailable (infra_fail — LLM/JSON error, logged, non-blocking)`;
  }
  const sorted = _severitySort(dissent);
  const head = sorted[0];
  const lines = [`[dissent] ${strategyId} → live (${regimes}): ${head.severity}: ${head.concern}`];
  for (const d of sorted.slice(1)) lines.push(`  - ${d.severity}: ${d.concern}`);
  return lines.slice(0, 10).join('\n');
}

/**
 * @param {Object} opts
 * @param {string} opts.strategyId
 * @param {string} [opts.actor]  — e.g. 'system:sunday-auto-approval'
 * @param {string} [opts.model]  — override the claude-bin model id
 * @param {function} [opts.runOneShotFn] — injection point for tests
 * @param {function} [opts.queryFn]      — injection point for tests
 * @returns {Promise<{strategyId:string, dissent:object[], infraFail:boolean}>}
 */
async function dissentOnPromotion({ strategyId, actor = null, model, runOneShotFn, queryFn } = {}) {
  const runFn   = runOneShotFn || runOneShotDeprivileged;
  const query   = queryFn || _query;
  const useModel = model || DEFAULT_MODEL;

  let context;
  try {
    context = await loadContext({ strategyId, query });
  } catch (e) {
    console.error(`[dissent] context load failed for ${strategyId}: ${e.message} — proceeding with minimal context`);
    context = {
      entry: null, instrumentClass: 'equity', qualifyingRegimes: [],
      run: null, runError: e.message, sleeves: [], sleevesError: null,
      liveCount: 0, neighbors: [], similarityNote: `context load failed: ${e.message}`,
      implPath: null, code: null, codeError: e.message,
    };
  }

  const prompt = buildPrompt({ strategyId, context });

  let { parsed, res } = await _attempt(prompt, { model: useModel, runOneShotFn: runFn });
  if (!parsed) {
    console.error(
      `[dissent] first attempt unusable for ${strategyId}` +
      `${res.error ? ` (infra: ${res.error})` : ' (malformed JSON)'} — retrying once`
    );
    ({ parsed, res } = await _attempt(prompt + '\n' + RETRY_REMINDER, { model: useModel, runOneShotFn: runFn }));
  }

  let dissent, infraFail;
  if (!parsed) {
    console.error(
      `[dissent_infra_fail] reviewer output unusable after retry for ${strategyId}` +
      `${res.error ? ` — ${res.error}` : ''}; recording empty dissent (non-veto — promotion already final)`
    );
    dissent = [];
    infraFail = true;
  } else {
    dissent = parsed;
    infraFail = false;
  }

  // INSERT — best-effort. A DB failure here must never propagate past this
  // function; the promotion this row would document is already committed.
  try {
    await query(
      `INSERT INTO promotion_dissents (strategy_id, actor, model, dissent, infra_fail)
       VALUES ($1, $2, $3, $4, $5)`,
      [strategyId, actor, useModel, JSON.stringify(dissent), infraFail]);
  } catch (e) {
    console.error(`[dissent] INSERT failed for ${strategyId}: ${e.message}`);
  }

  // Post to #botjohn-log — best-effort, reusing the shared webhook helper
  // (agent_registry lookup + Cloudflare-safe User-Agent). log-and-continue
  // when the webhook is unset/unreachable is correct behavior, not a bug.
  try {
    const summary = formatSummary({ strategyId, context, dissent, infraFail });
    const r = await postToChannel('botjohn', 'botjohn-log', summary);
    if (!r.ok) {
      console.error(`[dissent] webhook post failed for ${strategyId} — ${r.reason || r.status}: ${r.detail || r.body || ''}`);
    } else {
      console.error(`[dissent] posted to #botjohn-log for ${strategyId}`);
    }
  } catch (e) {
    console.error(`[dissent] webhook post threw for ${strategyId}: ${e.message}`);
  }

  return { strategyId, dissent, infraFail };
}

module.exports = {
  dissentOnPromotion,
  buildPrompt,
  validateDissentJson,
  formatSummary,
  loadContext,
  DEFAULT_MODEL,
};

if (require.main === module) {
  const sid = process.argv[2];
  if (!sid) {
    console.error('usage: node src/agent/curators/promotion_dissent.js <strategy_id>');
    process.exit(1);
  }
  dissentOnPromotion({ strategyId: sid, actor: process.env.OPENCLAW_DISSENT_ACTOR || 'manual:cli' })
    .then((r) => {
      console.log(JSON.stringify(r, null, 2));
      if (r.infraFail) {
        // An empty advisory row must never look like success on the CLI —
        // most likely cause is a claude-bin invocation failure (e.g. the
        // claudebot de-privilege target lacking credentials/HOME access) or
        // unparseable model output after the retry; see the [dissent_infra_fail]
        // WARN above for the underlying res.error.
        console.error('[dissent] WARN infra_fail=true — empty advisory dissent recorded; likely cause: claude-bin invocation failed or returned unparseable JSON (check claudebot credentials/HOME, POSTGRES_URI, network) — this is NOT a successful run');
        process.exit(2);
      }
      process.exit(0);
    })
    .catch((e) => {
      console.error('[dissent] FATAL:', e.message);
      console.error(e.stack);
      process.exit(1);
    });
}
