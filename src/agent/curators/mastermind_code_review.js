#!/usr/bin/env node
'use strict';

/**
 * mastermind_code_review.js — MasterMind (Opus 4.7 1M) re-examines STRATEGY
 * IMPLEMENTATION CODE for logic errors. New 2026-06-08.
 *
 * Distinct from comprehensive_review.js (which reviews trade *history* / sizing)
 * — this reads the actual `.py` and asks Opus whether the implementation
 * faithfully encodes its hypothesis and is free of common quant-coding bugs
 * (lookahead, wrong column, off-by-one lookback, inverted sign, signal-never-
 * fires, NaN handling). Candidates whose latest backtest has < 30 trades get a
 * low-trade-focused audit ("is the low count a BUG or genuinely rare?").
 *
 * Apply policy (operator decision 2026-06-08):
 *   - state == 'live'                 -> REPORT-ONLY (never auto-edit live code)
 *   - state in (candidate|staging)    -> gated auto-apply (separate flow; not here)
 * This module implements the REPORT-ONLY path used by:
 *   - the one-off live sweep (run now), and
 *   - the Sunday 14:00 slot for the live-strategy report half.
 *
 * CLI:
 *   node src/agent/curators/mastermind_code_review.js \
 *        [--state live|candidate|...] [--ids ID1,ID2] [--concurrency 3] \
 *        [--out logs/code_review_<date>.md] [--limit N] [--dry-run]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = process.env.OPENCLAW_DIR || '/root/openclaw';
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const IMPL_DIR      = path.join(OPENCLAW_DIR, 'src/strategies/implementations');
const WORKSPACE     = path.join(OPENCLAW_DIR, 'workspaces/default');

const VERDICTS        = new Set(['ok', 'fix_suggested', 'broken']);
const DIAGNOSES       = new Set(['bug', 'genuine', 'n/a']);
const DEFAULT_LOW_TRADE = parseInt(process.env.OPENCLAW_CODE_REVIEW_MIN_TRADES || '30', 10) || 30;

// ── Pure decision logic (unit-tested) ───────────────────────────────────────

/**
 * Which backtest shapes warrant scrutiny. Drives the low-trade re-eval.
 * @param {{total_trades:number|null}} backtest
 * @returns {string[]} concern tags
 */
function selectConcerns(backtest, opts = {}) {
  const thr = opts.lowTradeThreshold || DEFAULT_LOW_TRADE;
  const n = backtest ? backtest.total_trades : null;
  const concerns = [];
  if (n == null || Number(n) === 0) concerns.push('no_trades');
  if (n != null && Number(n) > 0 && Number(n) < thr) concerns.push('low_trade');
  return concerns;
}

/** Validate + normalise an Opus verdict object. Returns null if unusable. */
function validateVerdict(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  if (!VERDICTS.has(obj.verdict)) return null;
  return {
    strategy_id:            typeof obj.strategy_id === 'string' ? obj.strategy_id : null,
    verdict:                obj.verdict,
    faithful_to_hypothesis: typeof obj.faithful_to_hypothesis === 'boolean' ? obj.faithful_to_hypothesis : null,
    low_trade_diagnosis:    DIAGNOSES.has(obj.low_trade_diagnosis) ? obj.low_trade_diagnosis : null,
    issues:                 Array.isArray(obj.issues) ? obj.issues : [],
    proposed_fix:           typeof obj.proposed_fix === 'string' ? obj.proposed_fix : null,
    confidence:             typeof obj.confidence === 'number' ? obj.confidence : null,
  };
}

// ── IO helpers ──────────────────────────────────────────────────────────────

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

async function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

async function fetchBacktest(strategyId, query = _query) {
  const { rows } = await query(
    `SELECT total_trades, total_sharpe, total_max_dd_pct, total_return_pct,
            run_id::text AS run_id, run_at
       FROM strategy_backtest_runs
      WHERE strategy_id = $1 AND primary_window = TRUE
      ORDER BY run_at DESC LIMIT 1`,
    [strategyId]
  );
  if (!rows.length) return null;
  const bt = rows[0];
  const reg = await query(
    `SELECT regime_state, trade_count, sharpe, max_dd_pct, return_pct
       FROM strategy_backtest_regimes WHERE run_id = $1 ORDER BY regime_state`,
    [bt.run_id]
  ).catch(() => ({ rows: [] }));
  bt.regimes = reg.rows;
  return bt;
}

function buildPrompt({ strategyId, entry, code, backtest, concerns }) {
  const meta = entry && entry.metadata ? entry.metadata : {};
  const hypothesis = meta.description || meta.hypothesis || '(no description in manifest)';
  const btSummary = backtest
    ? `total_trades=${backtest.total_trades}, sharpe=${backtest.total_sharpe}, `
      + `max_dd_pct=${backtest.total_max_dd_pct}, return_pct=${backtest.total_return_pct}\n`
      + `per-regime: ${JSON.stringify(backtest.regimes || [])}`
    : '(no primary-window backtest on record)';
  const lowTradeBlock = concerns.includes('low_trade') || concerns.includes('no_trades')
    ? `\nIMPORTANT — this strategy has a SUSPICIOUSLY LOW trade count (${backtest ? backtest.total_trades : 0}).`
      + ` Decide whether that is a BUG (e.g. the signal predicate can essentially never be true,`
      + ` a column is misread/empty, a lookback exceeds the data, a sign is inverted, or NaN/exception`
      + ` paths silently drop signals) or GENUINELY a rare-event strategy. Set "low_trade_diagnosis".`
    : '';

  return [
    `You are MasterMindJohn, auditing a quant strategy's Python IMPLEMENTATION for logic errors.`,
    `Strategy id: ${strategyId}`,
    `State: ${entry ? entry.state : 'unknown'}`,
    ``,
    `Stated hypothesis / description:`,
    hypothesis,
    ``,
    `Latest backtest:`,
    btSummary,
    lowTradeBlock,
    ``,
    `Implementation (src/strategies/implementations/${path.basename(resolveImplPathSafe(strategyId, entry))}):`,
    '```python',
    code,
    '```',
    ``,
    `Audit ONLY for IMPLEMENTATION-LOGIC correctness — does the code faithfully encode the`,
    `hypothesis, and is it free of: lookahead/peeking, wrong/misread columns, off-by-one`,
    `lookbacks, inverted signs, signal-never-fires, unsafe NaN/exception handling that drops`,
    `signals, or universe/regime gating bugs? Do NOT comment on alpha quality or parameter`,
    `tuning — only whether the code does what it claims, correctly.`,
    ``,
    `Respond with ONE fenced \`\`\`json block, no prose, exactly this shape:`,
    '```json',
    `{`,
    `  "strategy_id": "${strategyId}",`,
    `  "verdict": "ok" | "fix_suggested" | "broken",`,
    `  "faithful_to_hypothesis": true | false,`,
    `  "low_trade_diagnosis": "bug" | "genuine" | "n/a",`,
    `  "issues": [{"severity":"high|med|low","kind":"lookahead|wrong_column|off_by_one|inverted_sign|no_signal|nan_handling|universe_gate|other","detail":"...","line_hint": <int|null>}],`,
    `  "proposed_fix": "concise description of the minimal fix, or null",`,
    `  "confidence": 0.0`,
    `}`,
    '```',
  ].join('\n');
}

// Helper used only for the filename hint in the prompt; never throws.
function resolveImplPathSafe(strategyId, entry) {
  const canonical = entry && entry.metadata && entry.metadata.canonical_file;
  return canonical || `${strategyId}.py`;
}

async function auditStrategy(strategyId, opts = {}) {
  const query    = opts.query   || _query;
  const manifest = opts.manifest || _loadManifest();
  const runOpus  = opts.runOpus || require('./_opus_oneshot').runOneShot;

  const entry = (manifest.strategies || {})[strategyId];
  if (!entry) return { strategy_id: strategyId, error: 'not_in_manifest' };
  const implPath = resolveImplPath(strategyId, manifest);
  if (!implPath) return { strategy_id: strategyId, state: entry.state, error: 'impl_not_found' };

  const code = fs.readFileSync(implPath, 'utf8');

  // Distinguish a genuine "no backtest row" from a query ERROR. Silently
  // mapping a failed fetch to null would feed Opus a false "no_trades" warning
  // (and the audit would chase a non-bug). Surface the error instead.
  let backtest = null, backtestError = null;
  try { backtest = await fetchBacktest(strategyId, query); }
  catch (e) { backtestError = e.message; }
  const concerns = backtestError ? []
    : (backtest === null ? ['no_backtest'] : selectConcerns(backtest));
  const prompt = buildPrompt({ strategyId, entry, code, backtest, concerns });

  const res = await runOpus({
    prompt,
    cwd: WORKSPACE,
    disallowedTools: ['Bash', 'Write', 'Edit', 'NotebookEdit'],  // read-only audit
    timeoutMs: 300_000,
  });
  const verdict = validateVerdict(require('./_opus_oneshot').parseJsonBlock(res.text));
  return {
    strategy_id:  strategyId,
    state:        entry.state,
    concerns,
    total_trades: backtest ? backtest.total_trades : null,
    sharpe:       backtest ? backtest.total_sharpe : null,
    verdict,
    raw:          verdict ? undefined : (res.text || '').slice(0, 600),
    backtest_error: backtestError,
    costUsd:      res.costUsd || 0,
    error:        res.error || backtestError || (verdict ? null : 'verdict_parse_failed'),
  };
}

// ── Driver ──────────────────────────────────────────────────────────────────

function strategyIdsByState(states, manifest = _loadManifest()) {
  const s = manifest.strategies || {};
  return Object.keys(s).filter(sid => states.includes(s[sid].state));
}

/**
 * Filter ids to those whose manifest state_since is within `days` of `nowMs`.
 * Used by the weekly 2PM candidate review to scope to "recent additions" so the
 * Opus spend stays bounded (vs auditing every candidate every week).
 */
function recentWithinDays(ids, days, manifest, nowMs) {
  if (!days) return ids;
  const s = manifest.strategies || {};
  const cutoff = nowMs - days * 86400 * 1000;
  return ids.filter(sid => {
    const t = Date.parse((s[sid] && s[sid].state_since) || '');
    return Number.isFinite(t) && t >= cutoff;
  });
}

async function runReview({ strategyIds, mode = 'report', concurrency = 3,
                           query = _query, runOpus, notify = () => {}, manifest } = {}) {
  manifest = manifest || _loadManifest();
  const findings = [];
  let totalCost = 0, cursor = 0, done = 0;

  const worker = async () => {
    while (true) {
      const i = cursor++;
      if (i >= strategyIds.length) return;
      const sid = strategyIds[i];
      try {
        const r = await auditStrategy(sid, { query, runOpus, manifest });
        findings[i] = r;
        totalCost += r.costUsd || 0;
      } catch (e) {
        findings[i] = { strategy_id: sid, error: e.message };
      }
      done += 1;
      const v = findings[i] && findings[i].verdict;
      notify(`[${done}/${strategyIds.length}] ${sid} -> ${v ? v.verdict : (findings[i].error || '?')}`);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, concurrency) }, () => worker()));

  // mode === 'report' is the only path implemented here. Gated auto-apply for
  // candidates is a separate, backtest-guarded flow (not run against live).
  return { mode, findings: findings.filter(Boolean), totalCost };
}

function renderReport(result) {
  const sev = (f) => {
    if (!f.verdict) return 4;                       // unparsed/error -> surface
    return { broken: 0, fix_suggested: 1, ok: 3 }[f.verdict.verdict] ?? 2;
  };
  const sorted = [...result.findings].sort((a, b) => sev(a) - sev(b));
  const lines = [];
  lines.push(`# MasterMind strategy-code review — ${result.findings.length} strategies, $${result.totalCost.toFixed(2)}`);
  lines.push('');
  const counts = { broken: 0, fix_suggested: 0, ok: 0, unparsed: 0 };
  for (const f of result.findings) {
    if (!f.verdict) counts.unparsed++;
    else counts[f.verdict.verdict]++;
  }
  lines.push(`**broken=${counts.broken}  fix_suggested=${counts.fix_suggested}  ok=${counts.ok}  unparsed/error=${counts.unparsed}**`);
  lines.push('');
  for (const f of sorted) {
    const v = f.verdict;
    const tag = v ? v.verdict.toUpperCase() : `ERROR(${f.error || '?'})`;
    lines.push(`## ${tag} — ${f.strategy_id} [${f.state || '?'}]  trades=${f.total_trades} sharpe=${f.sharpe}`);
    if (f.concerns && f.concerns.length) lines.push(`- concerns: ${f.concerns.join(', ')}`);
    if (v) {
      if (v.low_trade_diagnosis && v.low_trade_diagnosis !== 'n/a') lines.push(`- low_trade_diagnosis: **${v.low_trade_diagnosis}**`);
      if (v.faithful_to_hypothesis === false) lines.push(`- NOT faithful to hypothesis`);
      for (const is of v.issues) lines.push(`- [${is.severity}] ${is.kind}: ${is.detail}${is.line_hint != null ? ` (line ~${is.line_hint})` : ''}`);
      if (v.proposed_fix) lines.push(`- proposed_fix: ${v.proposed_fix}`);
    } else if (f.raw) {
      lines.push(`- unparsed Opus output: ${f.raw.replace(/\n/g, ' ').slice(0, 300)}`);
    }
    lines.push('');
  }
  return lines.join('\n');
}

module.exports = {
  selectConcerns, validateVerdict, resolveImplPath, fetchBacktest,
  buildPrompt, auditStrategy, runReview, renderReport, strategyIdsByState,
  recentWithinDays,
};

// ── CLI ───────────────────────────────────────────────────────────────────--
if (require.main === module) {
  const arg = (name, def = null) => {
    const i = process.argv.indexOf(name);
    if (i < 0) return def;
    const n = process.argv[i + 1];
    return (!n || n.startsWith('--')) ? true : n;
  };
  (async () => {
    // Load .env so POSTGRES_URI / CLAUDE_BIN are present when run standalone.
    try {
      for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
        const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
        if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
      }
    } catch (_) {}

    const idsArg = arg('--ids');
    const stateArg = arg('--state', 'live');
    const concurrency = parseInt(arg('--concurrency', '3'), 10) || 3;
    const limit = arg('--limit') ? parseInt(arg('--limit'), 10) : null;
    const outPath = arg('--out', `logs/code_review_${new Date().toISOString().slice(0, 10)}.md`);
    const dryRun = !!arg('--dry-run', false);

    const manifest = _loadManifest();
    const recentDays = arg('--recent-days') ? parseInt(arg('--recent-days'), 10) : null;
    let ids;
    if (idsArg && idsArg !== true) ids = String(idsArg).split(',').map(s => s.trim()).filter(Boolean);
    else ids = strategyIdsByState(String(stateArg).split(','), manifest);
    if (recentDays) ids = recentWithinDays(ids, recentDays, manifest, Date.now());
    if (limit) ids = ids.slice(0, limit);

    console.error(`[code-review] auditing ${ids.length} strategies (state=${stateArg}, concurrency=${concurrency}, dryRun=${dryRun})`);
    if (dryRun) { console.error(`[code-review] DRY RUN — ids: ${ids.join(', ')}`); process.exit(0); }

    const result = await runReview({
      strategyIds: ids, mode: 'report', concurrency, manifest,
      notify: (m) => console.error(`[code-review] ${m}`),
    });
    const report = renderReport(result);
    const abs = path.isAbsolute(outPath) ? outPath : path.join(OPENCLAW_DIR, outPath);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, report);
    console.error(`[code-review] wrote ${abs}  (cost $${result.totalCost.toFixed(2)})`);
    console.log(JSON.stringify({
      reviewed: result.findings.length,
      out: abs,
      cost_usd: Number(result.totalCost.toFixed(4)),
      counts: result.findings.reduce((a, f) => {
        const k = f.verdict ? f.verdict.verdict : 'unparsed';
        a[k] = (a[k] || 0) + 1; return a;
      }, {}),
    }, null, 2));
    process.exit(0);
  })().catch((e) => { console.error('[code-review] FATAL:', e.message, e.stack); process.exit(1); });
}
