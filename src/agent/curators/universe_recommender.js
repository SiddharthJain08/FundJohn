'use strict';

/**
 * universe_recommender.js — SP-2 Phase C Task 3.
 *
 * ⚠️ SUPERSEDED (2026-07-21, universe ladder campaign W3): universe
 * re-selection now SHRINKS the stored full-universe primary-run trades down
 * the tier ladder (scripts/run_universe_shrink.py — no per-candidate
 * re-runs, no LLM) with the prefer-largest select_tier. This curator's
 * per-candidate grid re-run (spawnSync universe_grid_cli below) and its
 * Opus pick stay only as a manual fallback; its timer
 * (openclaw-universe-recs.timer) remains disabled.
 *
 * MastermindJohn curator that runs every Saturday at 20:00 ET. For each live
 * strategy it:
 *   1. Builds a per-candidate backtest grid (4 cap-independent predicates) by
 *      shelling out to src/backtest/universe_grid_cli.py one candidate at a
 *      time.
 *   2. Packs the grid into the Opus prompt template at
 *      src/agent/prompts/subagents/universe-recommender.md.
 *   3. Calls Opus 4.7 1M to pick the best predicate.
 *   4. Persists the recommendation to strategy_universe_recommendations.
 *   5. Posts a plain-text summary to #universe-recs with an embedded footer
 *      that the Task 6 reaction handler parses.
 *
 * Budget guards:
 *   UNIVERSE_RECS_WEEKLY_BUDGET_USD   (default 400) — abort if cumulative cost
 *                                      would exceed this after each strategy.
 *   UNIVERSE_RECS_PER_STRATEGY_BUDGET_USD (default 8) — warn per strategy.
 *
 * Dry-run (--dry-run flag or dryRun=true):
 *   Builds the grid and prints grid_sha per strategy. Does NOT call Opus,
 *   Discord, or persist anything.
 *
 * 28-day adopt-skip:
 *   Strategies that already adopted a recommendation in the last 28 days are
 *   skipped to avoid churn.
 *
 * Temperature concern: _opus_oneshot.js passes no --temperature flag to
 * claude-bin. The spec requires temperature=0; since no claude-bin --temperature
 * option was verified, we rely on claude-bin defaults and document this as a
 * known gap. Determinism is preserved via grid SHA + prompt template SHA.
 *
 * CLI invocation note: the test suite (tests/test_universe_grid.py) invokes
 * the grid CLI as `python3 <absolute_path_to_cli>` from OPENCLAW_DIR, NOT
 * via `-m src.backtest.universe_grid_cli`. The `-m` form requires PYTHONPATH=src
 * and the file's own sys.path manipulation conflicts. We use the direct file
 * path per the verified working pattern.
 *
 * Live-strategy source: manifest.json (state==='live'), NOT strategy_registry.
 * strategy_registry.status only holds approved/pending_approval/deprecated —
 * there is no 'live' status. The manifest is the authoritative live/trading
 * set (same source as the resolver and sizer).
 */

const fs          = require('fs');
const path        = require('path');
const crypto      = require('crypto');
const { spawnSync } = require('child_process');

const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');
const { postToChannel }              = require('./_discord_webhook');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const PYTHON       = process.env.PYTHON_BIN   || 'python3';

// Absolute path to the CLI script — verified working invocation pattern
// (matches tests/test_universe_grid.py:_run_cli which also uses direct path).
const GRID_CLI     = path.join(OPENCLAW_DIR, 'src/backtest/universe_grid_cli.py');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const PROMPT_PATH   = path.join(OPENCLAW_DIR,
  'src/agent/prompts/subagents/universe-recommender.md');

// Cap-independent candidates only (C4 caveat: the 8 cap-gated predicates
// resolve empty for historical dates due to FMP Starter 403 on market_cap).
const CAP_INDEPENDENT = ['sp500', 'no_adr', 'no_otc', 'options_eligible_only'];
const CAP_UNVERIFIED  = [
  'r1000', 'r3000', 'large_cap', 'mid_cap', 'small_cap_liquid',
  'large_cap_options', 'mid_cap_options', 'top500_by_adv',
];

const WEEKLY_BUDGET_USD      = Number(process.env.UNIVERSE_RECS_WEEKLY_BUDGET_USD      || 400);
const PER_STRATEGY_BUDGET_USD = Number(process.env.UNIVERSE_RECS_PER_STRATEGY_BUDGET_USD || 8);

// Discord channel for universe-recs posts
const DISCORD_CHANNEL = process.env.DISCORD_UNIVERSE_RECS_CHANNEL || 'universe-recs';

// ── DB helper ────────────────────────────────────────────────────────────────

async function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) {
    _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  return _query._pool.query(sql, params);
}

// ── Strategy list ────────────────────────────────────────────────────────────

/**
 * Build a strategy descriptor from a loaded manifest, keyed by id.
 * Returns { id, name, description, implementation_path } or null if the
 * strategy does not exist in the manifest.
 */
function _strategyFromManifest(strategyId, manifest) {
  const record = (manifest.strategies || {})[strategyId];
  if (!record) return null;
  return {
    id:                  strategyId,
    name:                strategyId,
    description:         record.metadata?.description || '',
    implementation_path: record.metadata?.canonical_file || null,
  };
}

/**
 * Return all manifest strategies whose state === 'live', shaped as
 * { id, name, description, implementation_path }.
 *
 * NOTE: strategy_registry.status has no 'live' value — the authoritative
 * live/trading set is manifest.strategies[id].state. The resolver and sizer
 * both key off manifest state; we follow the same source of truth here.
 * Kept async so callers may use `await _liveStrategies()` unchanged.
 */
async function _liveStrategies() {
  const manifest = _loadManifest();
  const result = [];
  for (const [id, record] of Object.entries(manifest.strategies || {})) {
    if (record.state === 'live') {
      result.push({
        id,
        name:                id,
        description:         record.metadata?.description || '',
        implementation_path: record.metadata?.canonical_file || null,
      });
    }
  }
  result.sort((a, b) => a.id.localeCompare(b.id));
  return result;
}

// ── Manifest helpers ─────────────────────────────────────────────────────────

function _loadManifest() {
  try {
    return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf-8'));
  } catch (e) {
    throw new Error(`Failed to load manifest at ${MANIFEST_PATH}: ${e.message}`);
  }
}

/**
 * Read strategy source code. Returns { code, loc, filePath } or null on failure.
 * Instruction 4: read from manifest.strategies[id].metadata.canonical_file;
 * fall back to strategy_registry.implementation_path.
 */
function _readStrategyCode(strategyId, manifest, implPath = null) {
  const stratRecord = (manifest.strategies || {})[strategyId] || {};
  const canonicalFile = stratRecord.metadata?.canonical_file;

  // Try canonical_file first
  if (canonicalFile) {
    const absPath = path.join(OPENCLAW_DIR, 'src/strategies/implementations', canonicalFile);
    try {
      const code = fs.readFileSync(absPath, 'utf-8');
      return { code, loc: code.split('\n').length, filePath: absPath };
    } catch {
      // fall through to implementation_path
    }
  }

  // Fall back to strategy_registry.implementation_path
  if (implPath) {
    const absPath = path.isAbsolute(implPath)
      ? implPath
      : path.join(OPENCLAW_DIR, implPath);
    try {
      const code = fs.readFileSync(absPath, 'utf-8');
      return { code, loc: code.split('\n').length, filePath: absPath };
    } catch {
      // fall through
    }
  }

  return null;
}

/**
 * Instruction 5: current predicate from manifest or default 'sp500'.
 *
 * Returns the BARE predicate name (e.g. 'sp500', 'large_cap').  The manifest
 * stores the full module:attr form after a prior adoption
 * ("src.strategies.universe_default:large_cap"), so we strip the prefix if
 * a colon is present.  This keeps current_predicate and the no_change
 * candidate_predicate consistent with CANDIDATE_PREDICATES bare keys.
 */
function _currentPredicate(strategyId, manifest) {
  const stratRecord = (manifest.strategies || {})[strategyId] || {};
  const ref = stratRecord.metadata?.universe_filter_ref || 'sp500';
  // Strip module prefix: "src.strategies.universe_default:large_cap" → "large_cap"
  return ref.includes(':') ? ref.split(':').pop() : ref;
}

// ── Grid building ─────────────────────────────────────────────────────────────

/**
 * Run the grid CLI for one candidate. Returns parsed 8-key metrics dict or null.
 * Instruction 1: use direct file path, cwd=OPENCLAW_DIR, timeout=5min.
 */
function _runGridCli(strategyId, startIso, endIso, candidate) {
  const result = spawnSync(
    PYTHON,
    [
      GRID_CLI,
      '--strategy',         strategyId,
      '--start',            startIso,
      '--end',              endIso,
      '--resolver-override', candidate,
      '--metrics-json',
      '--seed',             '42',
    ],
    {
      cwd:      OPENCLAW_DIR,
      encoding: 'utf8',
      timeout:  5 * 60 * 1000,
      env:      process.env,
    }
  );

  if (result.error) {
    console.error(`[universe-rec] grid CLI spawn error for ${strategyId}/${candidate}: ${result.error.message}`);
    return null;
  }
  if (result.status !== 0) {
    const errMsg = (result.stderr || '').slice(0, 400);
    console.error(`[universe-rec] grid CLI exit ${result.status} for ${strategyId}/${candidate}: ${errMsg}`);
    return null;
  }

  // The CLI prints JSON as the last non-empty line of stdout
  const lines = (result.stdout || '').trim().split('\n').filter(l => l.trim());
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (line.startsWith('{')) {
      try {
        return JSON.parse(line);
      } catch (e) {
        console.error(`[universe-rec] JSON parse failed for ${strategyId}/${candidate}: ${e.message}`);
        return null;
      }
    }
  }
  console.error(`[universe-rec] no JSON output for ${strategyId}/${candidate}`);
  return null;
}

/**
 * Build the full grid for a strategy.
 * Returns { grid: [{name, ...metrics}], cap_unverified: [...] }.
 * C4: options_eligible_only will produce all-None metrics (0 trades); that's fine.
 */
function _buildGrid(strategyId, startIso, endIso) {
  const grid = [];
  for (const candidate of CAP_INDEPENDENT) {
    const metrics = _runGridCli(strategyId, startIso, endIso, candidate);
    grid.push({ name: candidate, ...(metrics || {
      sharpe: null, max_dd_pct: null, win_rate: null, mean_universe_size: null,
      trades_n: null, sortino: null, calmar: null, mean_holding_days: null,
    }) });
  }
  return { grid, cap_unverified: CAP_UNVERIFIED };
}

// ── Determinism helpers ───────────────────────────────────────────────────────

/**
 * SHA-256 of the canonicalized grid JSON (sorted keys per row, rows in stable order).
 */
function _gridSha(grid) {
  const canonical = JSON.stringify(
    grid.map(row => {
      const sorted = {};
      for (const k of Object.keys(row).sort()) sorted[k] = row[k];
      return sorted;
    })
  );
  return crypto.createHash('sha256').update(canonical, 'utf8').digest('hex');
}

/**
 * SHA-256 of the prompt template file content.
 */
function _templateSha(templateText) {
  return crypto.createHash('sha256').update(templateText, 'utf8').digest('hex');
}

// ── Prompt packing ────────────────────────────────────────────────────────────

/**
 * Pack the Opus prompt. Replaces {{var}} placeholders and renders the
 * {{#each grid}}...{{/each}} block.
 *
 * The template has:
 *   {{strategy_id}}, {{strategy_name}}, {{thesis}}, {{loc}},
 *   {{source_code}}, {{current_predicate}}, {{candidate_names}}
 *   {{#each grid}} | {{name}} | {{sharpe}} | ... | {{/each}}
 */
function _packPrompt(strategy, code, currentPredicate, grid, templateText) {
  const { loc, code: sourceCode } = code || { loc: 0, code: '(source unavailable)' };
  const candidateNames = grid.map(r => r.name).join(', ');

  // Render the {{#each grid}}...{{/each}} block
  const eachRe = /\{\{#each grid\}\}([\s\S]*?)\{\{\/each\}\}/;
  const eachMatch = eachRe.exec(templateText);
  let gridSection = '';
  if (eachMatch) {
    const rowTemplate = eachMatch[1];
    gridSection = grid.map(row => {
      return rowTemplate.replace(/\{\{(\w+)\}\}/g, (_, key) => {
        const val = row[key];
        if (val === null || val === undefined) return key === 'name' ? '(n/a)' : 'n/a';
        return String(val);
      });
    }).join('');
  }

  // Replace all {{var}} placeholders then splice in the rendered grid block.
  // {{source_code}} is substituted LAST so a strategy file containing literal
  // {{...}} tokens cannot be expanded as additional prompt variables.
  let prompt = templateText
    .replace(eachRe, gridSection)
    .replace(/\{\{strategy_id\}\}/g, strategy.id)
    .replace(/\{\{strategy_name\}\}/g, strategy.name || strategy.id)
    .replace(/\{\{thesis\}\}/g, strategy.description || '(no description)')
    .replace(/\{\{loc\}\}/g, String(loc))
    .replace(/\{\{current_predicate\}\}/g, currentPredicate)
    .replace(/\{\{candidate_names\}\}/g, candidateNames)
    .replace(/\{\{source_code\}\}/g, sourceCode || '(source unavailable)');

  return prompt;
}

// ── 28-day adopt-skip ─────────────────────────────────────────────────────────

async function _recentlyAdopted(strategyId) {
  const { rows } = await _query(
    `SELECT id FROM strategy_universe_recommendations
      WHERE strategy_id = $1
        AND adopted = TRUE
        AND adopted_at > NOW() - INTERVAL '28 days'
      LIMIT 1`,
    [strategyId]
  );
  return rows.length > 0;
}

// ── Discord post ──────────────────────────────────────────────────────────────

/**
 * Instruction 6: model EXACTLY on position_recommender.js:169 _postToDiscord.
 * Message is PLAIN TEXT and MUST embed the footer _footer: universe-rec:${recId}_
 * so the Task 6 reaction handler can parse `universe-rec:(\d+)` from content.
 */
async function _postDiscord(channelName, msg) {
  try {
    const r = await postToChannel('botjohn', channelName, msg);
    if (r.ok) return true;
    console.error(`[universe-rec] Discord post failed: ${r.reason || r.status}: ${r.detail || r.body || ''}`);
  } catch (e) {
    console.error(`[universe-rec] Discord post failed: ${e.message}`);
  }
  return false;
}

// ── Persistence ───────────────────────────────────────────────────────────────

async function _persist(strategyId, currentPredicate, decision, candidateSetId,
                        backtest_summary, costUsd) {
  const candidatePredicate = decision.choice === 'no_change'
    ? currentPredicate
    : decision.choice;
  const { rows } = await _query(
    `INSERT INTO strategy_universe_recommendations
       (strategy_id, current_predicate, candidate_predicate, candidate_set_id,
        backtest_summary, rationale, approved, mastermind_cost_usd)
     VALUES ($1, $2, $3, $4, $5::jsonb, $6, NULL, $7)
     RETURNING id`,
    [
      strategyId,
      currentPredicate,
      candidatePredicate,
      candidateSetId,
      JSON.stringify(backtest_summary),
      decision.rationale || null,
      costUsd,
    ]
  );
  return rows[0].id;
}

// ── Main run ──────────────────────────────────────────────────────────────────

/**
 * Main entry point.
 *
 * @param {object} opts
 * @param {boolean} [opts.dryRun=false]    — skip Opus/Discord/persist
 * @param {string}  [opts.strategyId]      — run only this strategy
 * @param {string}  [opts.weekEnding]      — ISO date for candidate_set_id (defaults to today)
 * @param {Function} [opts.notify]         — log callback(msg)
 * @returns {Promise<object>}
 */
async function run({ dryRun = false, strategyId = null, weekEnding = null,
                     notify = () => {} } = {}) {
  // Use ET date for the week tag so Sat 20:00 ET is not mis-tagged as Sunday UTC.
  const weekEndIso = weekEnding ||
    new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });

  // Candidate-set version from env (default 'v1'), e.g. "sp2c-v1-2026-05-24".
  const candidateSetVersion = process.env.UNIVERSE_RECS_CANDIDATE_SET_VERSION || 'v1';
  const candidateSetId = `sp2c-${candidateSetVersion}-${weekEndIso}`;

  // Backtest window: configurable lookback in days from weekEnding (default 365).
  const lookbackDays = parseInt(process.env.UNIVERSE_RECS_LOOKBACK_DAYS || '365', 10);
  const endDate   = new Date(weekEndIso);
  const startDate = new Date(endDate);
  startDate.setDate(startDate.getDate() - lookbackDays);
  const startIso = startDate.toISOString().slice(0, 10);
  const endIso   = weekEndIso;

  notify(`[universe-recs] run starting week=${weekEndIso} dry=${dryRun} window=${startIso}..${endIso}`);

  // Load manifest once
  let manifest;
  try {
    manifest = _loadManifest();
  } catch (e) {
    notify(`[universe-recs] FATAL: ${e.message}`);
    return { error: e.message, processed: 0 };
  }

  // Load prompt template once
  let templateText;
  try {
    templateText = fs.readFileSync(PROMPT_PATH, 'utf-8');
  } catch (e) {
    notify(`[universe-recs] FATAL: cannot read prompt template: ${e.message}`);
    return { error: e.message, processed: 0 };
  }
  const promptTemplateSha256 = _templateSha(templateText);
  const modelId = process.env.CURATOR_OPUS_MODEL || 'claude-opus-5[1m]';

  // Determine strategy list
  let strategies;
  if (strategyId) {
    // Operator override: look up by id in manifest (any state — operator may target
    // candidate/live/archived; manifest is the authoritative registry).
    const entry = _strategyFromManifest(strategyId, manifest);
    if (!entry) {
      return { error: `strategy '${strategyId}' not found in manifest`, processed: 0 };
    }
    strategies = [entry];
  } else {
    strategies = await _liveStrategies();
  }

  notify(`[universe-recs] ${strategies.length} strategies to evaluate`);

  let totalCostUsd  = 0;
  let processed     = 0;
  let skipped       = 0;
  const results     = [];

  for (const strategy of strategies) {
    // Weekly budget guard
    if (!dryRun && totalCostUsd >= WEEKLY_BUDGET_USD) {
      notify(`[universe-recs] BUDGET EXCEEDED ($${totalCostUsd.toFixed(2)} >= $${WEEKLY_BUDGET_USD}) — stopping`);
      break;
    }

    // 28-day adopt-skip
    if (!dryRun) {
      const recentlyAdoptedFlag = await _recentlyAdopted(strategy.id).catch(() => false);
      if (recentlyAdoptedFlag) {
        notify(`[universe-recs] skip ${strategy.id} — adopted within 28d`);
        skipped++;
        continue;
      }
    }

    notify(`[universe-recs] building grid for ${strategy.id} (${startIso}..${endIso})`);

    // Build grid (spawns CLI per candidate)
    let gridData;
    try {
      gridData = _buildGrid(strategy.id, startIso, endIso);
    } catch (e) {
      notify(`[universe-recs] grid build FAILED for ${strategy.id}: ${e.message}`);
      results.push({ strategy_id: strategy.id, error: e.message });
      continue;
    }

    const gridSha = _gridSha(gridData.grid);
    notify(`[universe-recs] ${strategy.id} grid_sha=${gridSha.slice(0, 12)}`);

    if (dryRun) {
      results.push({ strategy_id: strategy.id, grid_sha: gridSha, dry_run: true });
      processed++;
      continue;
    }

    // Read strategy source code
    const codeInfo = _readStrategyCode(strategy.id, manifest, strategy.implementation_path);
    if (!codeInfo) {
      notify(`[universe-recs] warn: could not read source for ${strategy.id}`);
    }

    const currentPredicate = _currentPredicate(strategy.id, manifest);

    // Pack prompt
    const prompt = _packPrompt(
      strategy, codeInfo || { code: '(source unavailable)', loc: 0 },
      currentPredicate, gridData.grid, templateText
    );

    // Call Opus
    notify(`[universe-recs] calling Opus for ${strategy.id}`);
    const res = await runOneShot({ prompt, timeoutMs: 30 * 60 * 1000 });

    if (res.error) {
      notify(`[universe-recs] Opus error for ${strategy.id}: ${res.error}`);
      results.push({ strategy_id: strategy.id, error: res.error });
      continue;
    }

    const costUsd = res.costUsd || 0;
    if (costUsd > PER_STRATEGY_BUDGET_USD) {
      notify(`[universe-recs] WARN: ${strategy.id} cost $${costUsd.toFixed(3)} > per-strategy budget $${PER_STRATEGY_BUDGET_USD}`);
    }
    totalCostUsd += costUsd;

    // Parse decision
    const decision = parseJsonBlock(res.text);
    if (!decision || typeof decision.choice !== 'string') {
      notify(`[universe-recs] bad Opus output for ${strategy.id}: ${res.text.slice(0, 200)}`);
      results.push({ strategy_id: strategy.id, error: 'opus_parse_failed', raw: res.text.slice(0, 200) });
      continue;
    }

    // Validate choice is in allowed set
    const validChoices = ['no_change', ...CAP_INDEPENDENT];
    if (!validChoices.includes(decision.choice)) {
      notify(`[universe-recs] invalid choice '${decision.choice}' for ${strategy.id} — skipping`);
      results.push({ strategy_id: strategy.id, error: `invalid_choice:${decision.choice}` });
      continue;
    }

    // Build backtest_summary (spec §2.3 determinism fields)
    const backtest_summary = {
      grid:                   gridData.grid,
      grid_sha256:            gridSha,
      candidate_set:          CAP_INDEPENDENT,
      cap_unverified:         CAP_UNVERIFIED,
      model_id:               modelId,
      prompt_template_sha256: promptTemplateSha256,
    };

    // Persist
    let recId;
    try {
      recId = await _persist(
        strategy.id, currentPredicate, decision,
        candidateSetId, backtest_summary, costUsd
      );
    } catch (e) {
      notify(`[universe-recs] persist failed for ${strategy.id}: ${e.message}`);
      results.push({ strategy_id: strategy.id, error: `persist_failed:${e.message}` });
      continue;
    }

    notify(`[universe-recs] persisted rec_id=${recId} for ${strategy.id} choice=${decision.choice}`);

    // Post to Discord
    const msg = _formatDiscordMessage(strategy, currentPredicate, decision, gridData.grid, recId);
    const posted = await _postDiscord(DISCORD_CHANNEL, msg);
    if (!posted) {
      notify(`[universe-recs] Discord post failed for ${strategy.id} rec_id=${recId}`);
    }

    results.push({
      strategy_id:        strategy.id,
      rec_id:             recId,
      choice:             decision.choice,
      current_predicate:  currentPredicate,
      confidence:         decision.confidence,
      cost_usd:           costUsd,
      posted,
      grid_sha:           gridSha,
    });
    processed++;
  }

  notify(`[universe-recs] done: processed=${processed} skipped=${skipped} total_cost=$${totalCostUsd.toFixed(3)}`);

  return {
    processed,
    skipped,
    total_cost_usd: totalCostUsd,
    week_ending:    weekEndIso,
    candidate_set_id: candidateSetId,
    results,
    dry_run: dryRun,
  };
}

// ── Discord message formatter ─────────────────────────────────────────────────

function _formatDiscordMessage(strategy, currentPredicate, decision, grid, recId) {
  const lines = [
    `**Universe Rec — ${strategy.id}**`,
    `Current: \`${currentPredicate}\` → Proposed: \`${decision.choice}\``,
    `Confidence: ${decision.confidence != null ? (decision.confidence * 100).toFixed(0) + '%' : 'n/a'}`,
    `Rationale: ${(decision.rationale || '(none)').slice(0, 400)}`,
    '',
    '**Grid:**',
  ];

  // Add table header
  lines.push('| Candidate | Sharpe | MaxDD% | WinRate | Trades | Sortino | Calmar |');
  lines.push('|---|---|---|---|---|---|---|');
  for (const row of grid) {
    const fmt = v => v !== null && v !== undefined ? String(v) : 'n/a';
    lines.push(
      `| \`${row.name}\` | ${fmt(row.sharpe)} | ${fmt(row.max_dd_pct)} | ${fmt(row.win_rate)} | ${fmt(row.trades_n)} | ${fmt(row.sortino)} | ${fmt(row.calmar)} |`
    );
  }

  // React ✅ to approve, ❌ to reject
  lines.push('');
  lines.push('React ✅ to approve · ❌ to reject · ⏸ to defer');

  // REQUIRED footer for Task 6 reaction handler: parses universe-rec:(\d+)
  lines.push(`_footer: universe-rec:${recId}_`);

  return lines.join('\n').slice(0, 1900);
}

// ── Exports ───────────────────────────────────────────────────────────────────

module.exports = {
  run,
  _gridSha,
  _packPrompt,
  _buildGrid,
  _runGridCli,
  _currentPredicate,
  _readStrategyCode,
  _liveStrategies,
  _strategyFromManifest,
  _templateSha,
  _formatDiscordMessage,
};
