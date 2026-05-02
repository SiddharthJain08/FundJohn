'use strict';

/**
 * saturday_brain.js — Consolidated Saturday research run (Sat 10:00 ET timer).
 *
 * Replaces the legacy split:
 *   openclaw-mastermind-corpus.timer (Sat 10:00 ET)
 *   openclaw-paper-expansion.timer   (Sun 12:00 UTC)
 *
 * Eight phases, each persistent (saturday_runs row):
 *   0 preflight    capability map + manifest snapshot + vault read
 *   1 expand       Opus + WebSearch finds NEW source feeds
 *   2 sweep        Python ingestion: arxiv + openalex + expanded_sources
 *   3 rate         mastermind corpus rating with implementability axis +
 *                  promotion to research_candidates
 *   4 hunt         paperhunter fan-out across implementable_candidates
 *   5 tier         data_tier_filter assigns A/B/C
 *   6 code         Tier-A synchronous strategycoder + validate + backtest
 *   6.5 ideate     strategist-ideator from data inventory
 *   7 stage        Tier-B push to STAGING (operator-gated; Saturday brain
 *                  only writes the manifest entry + data_requirements_planned)
 *   8 closeout     vault notes + Discord summary + finalize saturday_runs row
 *
 * Tier-C candidates surface as deferred paper notes in the vault — no
 * manifest entry, no DB-side strategy row, just a graph-visible reminder
 * that a provider integration would unlock the strategy.
 */

const fs   = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const PYTHON       = process.env.PYTHON_BIN || 'python3';
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');

const MastermindCurator   = require('./mastermind');
const expansionIngestor   = require('./paper_expansion_ingestor');
const dataTierFilter      = require('./data_tier_filter');
const vaultLinker         = require('./vault_linker');
const ResearchOrchestrator = require('../research/research-orchestrator');
const { ingestScreenerCandidates } = require('../../pipeline/alpaca_screener');

const DEFAULT_BUDGET_USD       = 400;
const DEFAULT_HUNTER_FANOUT_N  = 200;
const DEFAULT_HUNTER_CONCURR   = 8;
const DEFAULT_TIER_A_CAP       = 80;       // synchronous strategycoder runs
const TIER_A_PER_RUN_USD       = 0.50;     // strategycoder ceiling estimate
const SAFETY_RESERVE_USD       = 14;       // leave headroom for retries

// ── Pool helper (pg singleton) ──────────────────────────────────────────────
function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

// ── Phase 0: pre-flight ─────────────────────────────────────────────────────
async function _preflight(notify) {
  const capabilityMap = await dataTierFilter.buildCapabilityMap(_query);
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const existingStrategyIds = Object.keys(manifest.strategies || {});
  const lastRun = (await _query(
    `SELECT run_id, started_at::text AS started_at
       FROM saturday_runs
      WHERE status = 'completed'
      ORDER BY started_at DESC LIMIT 1`
  )).rows[0] || null;
  notify(`preflight: ${Object.keys(capabilityMap.columns).length} backfilled cols, `
    + `${existingStrategyIds.length} manifest strategies, `
    + `last completed run: ${lastRun?.started_at || 'never'}`);
  return {
    capabilityMap,
    manifest,
    existingStrategyIds,
    lastRunStartedAt: lastRun?.started_at || null,
  };
}

// ── Phase 1: source expansion (Opus discovers new feeds) ────────────────────
async function _expand(opts, notify) {
  // In dry-run mode skip the Opus + WebSearch call entirely. The paper
  // expansion ingestor's own --dry-run flag still issues the LLM call
  // (it just skips DB writes); for saturday-brain dry-run we want to
  // verify the *orchestration shape* without spending tokens.
  if (opts.dryRun) {
    notify('source expansion: DRY RUN — skipping Opus call');
    return { expansionId: null, sourcesDiscovered: 0, costUsd: 0 };
  }
  notify('source expansion (Opus + WebSearch) starting...');
  const result = await expansionIngestor.run({
    dryRun: false,
    notify: (m) => notify(`  expand: ${m}`),
  });
  return {
    expansionId: result.expansion_id || result.expansionId,
    sourcesDiscovered: (result.sources_discovered && Array.isArray(result.sources_discovered))
                          ? result.sources_discovered.length
                          : (Number(result.sources_discovered) || 0),
    costUsd: Number(result.costUsd || 0),
  };
}

// ── Phase 2: historical / incremental sweep ─────────────────────────────────
function _runPython(scriptRelPath, args, notify) {
  return new Promise((resolve) => {
    const full = path.join(OPENCLAW_DIR, scriptRelPath);
    const child = spawn(PYTHON, [full, ...args], {
      cwd: OPENCLAW_DIR,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '', stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });
    child.on('close', (code) => {
      const tail = (stdout + stderr).split('\n').filter(Boolean).slice(-3).join(' | ');
      notify(`  ${path.basename(scriptRelPath)}: exit=${code} ${tail.slice(0, 200)}`);
      resolve({ code, stdout, stderr });
    });
    child.on('error', (err) => {
      notify(`  ${scriptRelPath}: spawn error ${err.message}`);
      resolve({ code: -1, stdout: '', stderr: err.message });
    });
  });
}

async function _sweep({ sinceIso, allTime, expansionId, dryRun }, notify) {
  // Resolve window flag for the ingestors. since-iso wins, then all-time,
  // then default to all-time (the brain has no last-run anchor on first run).
  const windowArgs = sinceIso ? ['--since-iso', sinceIso]
                              : (allTime ? ['--all-time'] : ['--all-time']);
  notify(`sweep: window=${windowArgs.join(' ')}`);
  if (dryRun) {
    notify('sweep: DRY RUN — skipping Python ingestor invocations');
    const cur = await _query(`SELECT COUNT(*)::int AS n FROM research_corpus`);
    return { ingested: 0, corpusSize: cur.rows[0].n };
  }

  // Pre-count corpus size so we can report new ingest count.
  const beforeQ = await _query(`SELECT COUNT(*)::int AS n FROM research_corpus`);
  const before = beforeQ.rows[0].n;

  // Ingestors run sequentially; arXiv first (cheapest, faster), then OpenAlex
  // (slower but higher signal venues), then expanded_sources.py (parses the
  // feeds Phase 1 just discovered).
  await _runPython('src/ingestion/arxiv_discovery.py',
                   [...windowArgs, '--max-per-cat', '200', '--no-legacy'], notify);
  await _runPython('src/ingestion/openalex_discovery.py',
                   [...windowArgs, '--limit-per-venue', '500'], notify);
  if (expansionId) {
    await _runPython('src/ingestion/expanded_sources.py',
                     ['--expansion-id', expansionId, '--max-per-source', '100'], notify);
  }

  const afterQ = await _query(`SELECT COUNT(*)::int AS n FROM research_corpus`);
  const after = afterQ.rows[0].n;
  const ingested = Math.max(0, after - before);
  notify(`sweep: corpus ${before} → ${after} (+${ingested})`);
  return { ingested, corpusSize: after };
}

// ── Phase 3: corpus rating with implementability axis ───────────────────────
async function _rate(opts, notify) {
  // Dry-run: skip Opus calls entirely. Curator's own --dry-run still
  // invokes the LLM (its semantics are "skip DB writes, emit calibration").
  // For brain dry-run we want zero token spend.
  if (opts.dryRun) {
    notify('corpus rate: DRY RUN — skipping Opus call');
    return { runId: null, rated: 0, buckets: {}, implementableN: 0, promoted: 0, costUsd: 0 };
  }
  const curator = new MastermindCurator();
  const result = await curator.run({
    dryRun: false,
    batchSize: 100,
    notify: (m) => notify(`  rate: ${m}`),
  });
  let promotion = { promoted: 0 };
  if (result.runId) {
    promotion = await curator.promoteHighBucket({
      runId: result.runId, maxToPromote: 600,
    });
    notify(`rate: promoted ${promotion.promoted} (${(promotion.spotChecked||0)} spot-check) to research_candidates`);
  }
  return {
    runId: result.runId,
    rated: result.outputCount,
    buckets: result.buckets || {},
    implementableN: (result.buckets && result.buckets.implementable_candidate) || 0,
    promoted: promotion.promoted || 0,
    costUsd: Number(result.costUsd || 0),
  };
}

// ── Phase 4: paperhunter fan-out across implementable candidates ────────────
async function _hunt(maxFanout, opts, notify) {
  // Pull the top-N research_candidates that came from the just-completed
  // rating run (priority desc) AND haven't been paperhunter-extracted yet.
  // We scope to curator-submitted rows from this run window.
  const { rows } = await _query(
    `SELECT candidate_id::text AS candidate_id
       FROM research_candidates
      WHERE submitted_by IN ('curator','curator_spotcheck','ideator')
        AND (hunter_result_json IS NULL OR hunter_result_json::text IN ('null','{}'))
        AND status IN ('pending','processing')
      ORDER BY priority DESC, submitted_at DESC
      LIMIT $1`,
    [maxFanout]
  );
  if (rows.length === 0) {
    notify('hunt: nothing to extract (no pending candidates without hunter results)');
    return { run: 0, results: [] };
  }
  notify(`hunt: spawning paperhunter on ${rows.length} candidates (concurrency ${DEFAULT_HUNTER_CONCURR})`);

  if (opts.dryRun) {
    notify('hunt: DRY RUN — skipping spawn, returning empty results');
    return { run: 0, results: [], candidateIds: rows.map(r => r.candidate_id) };
  }
  const orch = new ResearchOrchestrator();
  const results = await orch.runHunterFanout(
    rows.map(r => r.candidate_id),
    { concurrency: DEFAULT_HUNTER_CONCURR, notify: (m) => notify(`  hunt: ${m}`) }
  );
  return { run: rows.length, results, candidateIds: rows.map(r => r.candidate_id) };
}

// ── Phase 5: data-tier the hunter results ───────────────────────────────────
async function _tier(huntResults, capabilityMap, notify) {
  const tiers = { A: [], B: [], C: [] };
  if (!huntResults || huntResults.length === 0) {
    notify('tier: no hunter results to tier');
    return { tiers, decisions: [] };
  }

  const decisions = [];
  for (const result of huntResults) {
    if (!result || !result.candidate_id) continue;
    if (result.rejection_reason_if_any) {
      // PaperHunter rejected the paper — no tier assigned, skip.
      continue;
    }
    const decision = dataTierFilter.tierCandidate(result, capabilityMap);
    decisions.push({ candidateId: result.candidate_id, hunterResult: result, decision });
    tiers[decision.tier].push({ candidateId: result.candidate_id, hunterResult: result, decision });
    // Persist tier label.
    await _query(
      `UPDATE research_candidates SET data_tier = $1 WHERE candidate_id = $2`,
      [decision.tier, result.candidate_id]
    ).catch(() => {});
  }
  notify(`tier: A=${tiers.A.length} B=${tiers.B.length} C=${tiers.C.length}`);
  return { tiers, decisions };
}

// ── Phase 6: synchronous code+backtest for Tier A ───────────────────────────
async function _code(tierA, opts, runRowState, notify) {
  if (tierA.length === 0) {
    notify('code: no Tier-A candidates');
    return { coded: 0, failed: 0, strategies: [] };
  }
  const orch = new ResearchOrchestrator();
  // Compute remaining synchronous-coding budget. Reserve 5% for vault + posts.
  const budget = opts.maxBudgetUsd ?? DEFAULT_BUDGET_USD;
  const usedSoFar = runRowState.cost_usd_so_far || 0;
  const remaining = budget - usedSoFar - SAFETY_RESERVE_USD;
  const maxByBudget = Math.max(0, Math.floor(remaining / TIER_A_PER_RUN_USD));
  const cap = Math.min(DEFAULT_TIER_A_CAP, maxByBudget, tierA.length);
  notify(`code: budget remaining ~$${remaining.toFixed(2)}, capping Tier-A coding at ${cap}/${tierA.length}`);

  if (opts.dryRun || cap === 0) {
    return { coded: 0, failed: 0, strategies: [] };
  }

  const strategies = [];
  let coded = 0, failed = 0;
  for (let i = 0; i < cap; i++) {
    const { hunterResult, candidateId } = tierA[i];
    const sid = hunterResult.strategy_id || null;
    if (!sid) { failed++; continue; }
    notify(`  code [${i+1}/${cap}] ${sid}`);
    try {
      // Build the implementation_queue item shape _codeFromQueue expects.
      const item = {
        candidate_id: candidateId,
        strategy_spec: { ...hunterResult, strategy_id: sid, candidate_id: candidateId },
      };
      const outcome = await orch._codeFromQueue(item, undefined, undefined, {
        onPhase: (phase, pct) => notify(`    ${phase} ${pct}%`),
      });
      if (outcome && outcome.promoted) {
        coded++;
        strategies.push({ strategy_id: sid, hunterResult, backtest: outcome.backtest_result });
      } else {
        failed++;
      }
    } catch (e) {
      failed++;
      notify(`    error: ${e.message}`);
    }
  }
  return { coded, failed, strategies };
}

// ── Phase 6.5: strategist-ideator (data-aware idea generation) ──────────────
async function _ideate(opts, notify) {
  if (opts.dryRun) { notify('ideate: DRY RUN — skipped'); return { generated: 0 }; }
  try {
    const swarm = require('../subagents/swarm');
    const { v4: uuidv4 } = require('uuid');
    notify('ideate: strategist-ideator generating ideas from current data inventory...');
    await swarm.init({
      type:      'strategist-ideator',
      mode:      'IDEATE',
      workspace: path.join(OPENCLAW_DIR, 'workspaces/default'),
      threadId:  uuidv4(),
      prompt:    'Generate 3–5 novel strategy ideas based on the current memory ' +
                 'files and the data we have backfilled. Insert each into ' +
                 "research_candidates with submitted_by='ideator'. Avoid duplicating " +
                 'existing manifest strategies.',
    });
    return { generated: 1 };  // ideator's own count goes via its own logging
  } catch (e) {
    notify(`ideate: error — ${e.message}`);
    return { generated: 0, error: e.message };
  }
}

// ── Phase 7: Tier-B → STAGING (operator-gated; nothing fires automatically) ──
async function _stage(tierB, opts, notify) {
  if (tierB.length === 0) {
    notify('stage: no Tier-B candidates');
    return { staged: 0, strategies: [] };
  }
  if (opts.dryRun) {
    notify(`stage: DRY RUN — would stage ${tierB.length} Tier-B candidates`);
    return { staged: 0, strategies: tierB.map(t => t.hunterResult.strategy_id) };
  }

  // Acquire the cross-process manifest lock for the entire read-modify-write
  // cycle. Concurrent strategycoder + lifecycle.py + dashboard writes are
  // serialized via the shared lockfile contract in src/lib/manifest_lock.js
  // + src/strategies/_manifest_lock.py. DB upserts run inside the lock too,
  // so the in-memory manifest snapshot used for implPath stays consistent
  // with what eventually lands on disk.
  const { withManifestLock } = require('../../lib/manifest_lock');
  const staged = [];
  const now = new Date().toISOString();
  await withManifestLock(MANIFEST_PATH, async (manifest) => {
    manifest.strategies = manifest.strategies || {};
    for (const { hunterResult, candidateId, decision } of tierB) {
      const sid = hunterResult.strategy_id;
      if (!sid) continue;
      if (manifest.strategies[sid]) {
        // Already in manifest — don't clobber. Just add a history note and
        // attach the data_requirements_planned to strategy_registry.
        manifest.strategies[sid].history = manifest.strategies[sid].history || [];
        manifest.strategies[sid].history.push({
          from_state: manifest.strategies[sid].state,
          to_state:   manifest.strategies[sid].state,
          timestamp:  now,
          actor:      'saturday_brain',
          reason:     'Tier-B re-tiered; awaiting operator data-fetch approval',
          metadata:   { decision, candidate_id: candidateId },
        });
      } else {
        manifest.strategies[sid] = {
          state:       'staging',
          state_since: now,
          metadata: {
            canonical_file: `${sid.toLowerCase()}.py`,
            class:          sid,
            description:    (hunterResult.hypothesis_one_liner || sid).slice(0, 280),
          },
          history: [{
            from_state: null,
            to_state:   'staging',
            timestamp:  now,
            actor:      'saturday_brain',
            reason:     'Tier-B: data fetchable but not backfilled — awaiting operator approval',
            metadata:   { decision, candidate_id: candidateId },
          }],
        };
      }

      // Canonical strategy_registry upsert — handles implementation_path
      // (NOT NULL), preserves status='approved' on conflict, JSONB casts.
      // See src/lib/strategy_registry_upsert.js.
      const { upsertStrategyRegistry } = require('../../lib/strategy_registry_upsert');
      await upsertStrategyRegistry({
        id: sid,
        name: manifest.strategies[sid].metadata.class,
        implementationPath: `src/strategies/implementations/${manifest.strategies[sid].metadata.canonical_file}`,
        status: 'pending_approval',
        dataRequirementsPlanned: decision.provider_route || [],
        parameters: { candidate_id: candidateId, hypothesis: hunterResult.hypothesis_one_liner },
        universe: [String(hunterResult.universe || 'SP500').replace(/\s+/g, '')],
        dbQuery: _query,
      }).catch((e) => notify(`  stage[${sid}]: registry upsert failed — ${e.message}`));

      staged.push(sid);
    }
    manifest.updated_at = now;
    return manifest;
  }, { actor: 'saturday_brain._stage' });
  notify(`stage: ${staged.length} candidate(s) pushed to STAGING (manifest written)`);
  return { staged: staged.length, strategies: staged };
}

// ── Phase 8: vault notes + Discord summary ──────────────────────────────────
async function _closeout(allRunData, opts, notify) {
  const { runRow, tierResults, codeResults, stageResults, manifest, paperRows } = allRunData;
  if (opts.dryRun) {
    notify(`closeout: DRY RUN — skipping vault writes + Discord post`);
    return;
  }
  // Vault notes — best-effort. Walk every tiered candidate.
  let paperNotes = 0, deferredNotes = 0, strategyNotes = 0;
  for (const { hunterResult, decision, candidateId } of tierResults.decisions || []) {
    const paperRow = paperRows.get(candidateId) || {};
    const sid = hunterResult.strategy_id;
    const linkedStrategies = sid ? [sid] : [];
    const linkedCols = (hunterResult.data_requirements?.required) || [];
    if (decision.tier === 'C') {
      const noteFile = vaultLinker.writeDeferredPaperNote(
        paperRow, decision.missing_columns, decision.unlock_provider_estimate
      );
      if (noteFile) deferredNotes++;
    } else {
      const noteFile = vaultLinker.writePaperNote(
        paperRow, paperRow.rating || {}, hunterResult, decision.tier,
        { linkedStrategies, linkedDataCategories: linkedCols }
      );
      if (noteFile) paperNotes++;
    }
    if (sid && (decision.tier === 'A' || decision.tier === 'B')) {
      const slug = `${(paperRow.published_date || '').slice(0,10)}-${(paperRow.title || '').toLowerCase().replace(/[^a-z0-9]+/g,'-').slice(0,80)}`;
      const stratNote = vaultLinker.writeStrategyNote(
        sid,
        manifest.strategies?.[sid] || null,
        hunterResult,
        slug ? [slug] : [],
      );
      if (stratNote) strategyNotes++;
    }
  }
  // Run summary note.
  const summaryFile = vaultLinker.writeRunSummary(runRow, {
    tier_a_strategies: codeResults.strategies?.map(s => s.strategy_id) || [],
    tier_b_strategies: stageResults.strategies || [],
  });
  notify(`closeout: ${paperNotes} paper, ${deferredNotes} deferred, ${strategyNotes} strategy notes; summary=${summaryFile ? 'yes' : 'no'}`);

  // Discord summary post: routed via MastermindJohn → #research-feed
  // (2026-05-02 — was previously routed via DataBot → #strategy-memos
  // which conflated brain digests with comprehensive_review per-strategy
  // memos. ResearchJohn/ResearchDesk was retired in the same change;
  // mastermind now owns both #research-feed and #strategy-memos).
  try {
    const r = await _query(
      `SELECT webhook_urls FROM agent_registry WHERE id='mastermind'`
    );
    const url = ((r.rows[0]?.webhook_urls) || {})['research-feed'];
    if (url) {
      const lines = [
        `🧠 **Saturday brain — ${runRow.started_at?.toString().slice(0,10) || ''}**`,
        `Cost: $${(runRow.cost_usd || 0).toFixed(2)} · Status: ${runRow.status}`,
        `Papers ingested: ${runRow.papers_ingested ?? 0} · Rated: ${runRow.papers_rated ?? 0}`,
        `Implementable: ${runRow.implementable_n ?? 0} · PaperHunters: ${runRow.paperhunters_run ?? 0}`,
        `**Tier A (coded):** ${runRow.coded_synchronous ?? 0} / ${runRow.tier_a_count ?? 0}`,
        `**Tier B (staging):** ${runRow.tier_b_count ?? 0} — awaiting operator approval`,
        `**Tier C (deferred):** ${runRow.tier_c_count ?? 0}`,
      ].join('\n');
      await _postWebhook(url, lines);
      notify('closeout: Discord summary posted to #research-feed (mastermind)');
    } else {
      notify('closeout: skipping Discord post — mastermind has no research-feed webhook (run johnbot to initialize)');
    }
  } catch (e) {
    notify(`closeout: Discord post failed — ${e.message}`);
  }
}

function _postWebhook(url, content) {
  return new Promise((resolve) => {
    const https = require('https');
    const u = new URL(url);
    const body = JSON.stringify({ content: String(content).slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.on('data', () => {}); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', () => resolve(0));
    req.write(body); req.end();
  });
}

// ── Top-level run() ─────────────────────────────────────────────────────────
async function run(opts = {}) {
  const notify = opts.notify || (() => {});
  const { rows: createRows } = await _query(
    `INSERT INTO saturday_runs (status, current_phase) VALUES ('running','preflight') RETURNING run_id`
  );
  const runId = createRows[0].run_id;
  notify(`saturday_run ${runId.slice(0,8)} started${opts.dryRun ? ' (DRY RUN)' : ''}`);

  let totalCost = 0;
  const updatePhase = async (phase, partials = {}) => {
    const sets = [];
    const params = [runId];
    let pi = 1;
    sets.push(`current_phase = $${++pi}`); params.push(phase);
    for (const [k, v] of Object.entries(partials)) {
      sets.push(`${k} = $${++pi}`);
      params.push(typeof v === 'object' ? JSON.stringify(v) : v);
    }
    if (sets.length) {
      await _query(
        `UPDATE saturday_runs SET ${sets.join(', ')} WHERE run_id = $1`,
        params
      ).catch(() => {});
    }
  };

  let preflightData, expandData, sweepData, rateData, huntData, tierData,
      codeResults, ideateData, stageResults;
  try {
    // Phase 0
    preflightData = await _preflight(notify);

    // Phase 0.5: Alpaca screener augmentation. Pulls top movers + most-actives
    // from the broker and inserts new symbols into universe_config with
    // source='alpaca_screener', active=false. Runs best-effort: if the CLI
    // misbehaves we log and continue rather than block the rest of the cycle
    // (the screener data is augmentative — the corpus pipeline still works).
    try {
      const screenerOut = await ingestScreenerCandidates({
        topN:   parseInt(process.env.ALPACA_SCREENER_TOP_N || '50', 10),
        dryRun: !!opts.dryRun,
      });
      notify(`screener: ${screenerOut.discovered} discovered, `
            + `${screenerOut.inserted} new in universe_config, `
            + `${screenerOut.skipped} already present`);
    } catch (screenerErr) {
      notify(`screener: skipped (${screenerErr.message})`);
    }

    await updatePhase('expand', {
      context_snapshot: {
        capability_summary: {
          backfilled_columns: Object.keys(preflightData.capabilityMap.columns),
          fetchable_only:     Object.keys(preflightData.capabilityMap.fetchable_only),
        },
        manifest_strategies: preflightData.existingStrategyIds.length,
        last_run_started_at: preflightData.lastRunStartedAt,
      },
    });

    // Phase 1
    expandData = await _expand(opts, notify);
    totalCost += expandData.costUsd;
    await updatePhase('sweep', {
      sources_discovered: expandData.sourcesDiscovered,
      cost_usd: totalCost,
    });

    // Phase 2
    // Resolve since-window. Minimum 30 days back so that on quiet weeks
    // we still fan out across enough venue history to actually find new
    // papers — OpenAlex's indexing for SSRN/NBER/JF/RFS/JFE/JFQA/QF lags
    // by ~2-3 weeks, so a "since-last-Saturday" 7-day window returned 0
    // papers on 2026-05-02 (regression vs. the 2026-04-25 all-time
    // first-run that ingested ~3500). Duplicates are deduped by
    // research_corpus.source_url ON CONFLICT — the cost of a wider
    // window is only API quota, not duplicate rows.
    const MIN_SINCE_DAYS = 30;
    const _ms = Date.now() - MIN_SINCE_DAYS * 86400 * 1000;
    const _floorIso = new Date(_ms).toISOString().slice(0, 10);
    let sinceIso = opts.sinceIso
      || (preflightData.lastRunStartedAt && !opts.allTime
            ? preflightData.lastRunStartedAt.slice(0, 10)
            : null);
    if (sinceIso && sinceIso > _floorIso && !opts.allTime) {
      notify(`sweep: sinceIso=${sinceIso} is < ${MIN_SINCE_DAYS}d ago; widening to ${_floorIso} to avoid OpenAlex indexing-lag holes`);
      sinceIso = _floorIso;
    }
    sweepData = await _sweep({ sinceIso, allTime: opts.allTime, expansionId: expandData.expansionId, dryRun: opts.dryRun }, notify);
    await updatePhase('rate', { papers_ingested: sweepData.ingested });

    // Phase 3
    rateData = await _rate(opts, notify);
    totalCost += rateData.costUsd;
    await updatePhase('hunt', {
      papers_rated:     rateData.rated,
      implementable_n:  rateData.implementableN,
      cost_usd:         totalCost,
    });

    // Phase 4
    const fanoutN = Math.min(DEFAULT_HUNTER_FANOUT_N, rateData.promoted || DEFAULT_HUNTER_FANOUT_N);
    huntData = await _hunt(fanoutN, opts, notify);
    // Approximate paperhunter cost: $0.15 × runs.
    totalCost += huntData.run * 0.15;
    await updatePhase('tier', {
      paperhunters_run: huntData.run,
      cost_usd:         totalCost,
    });

    // Phase 5
    tierData = await _tier(huntData.results, preflightData.capabilityMap, notify);
    await updatePhase('code', {
      tier_a_count: tierData.tiers.A.length,
      tier_b_count: tierData.tiers.B.length,
      tier_c_count: tierData.tiers.C.length,
    });

    // Phase 6
    codeResults = await _code(tierData.tiers.A, opts, { cost_usd_so_far: totalCost }, notify);
    totalCost += codeResults.coded * TIER_A_PER_RUN_USD;
    await updatePhase('ideate', {
      coded_synchronous: codeResults.coded,
      coded_failed:      codeResults.failed,
      cost_usd:          totalCost,
    });

    // Phase 6.5
    ideateData = await _ideate(opts, notify);
    await updatePhase('stage', {});

    // Phase 7
    stageResults = await _stage(tierData.tiers.B, opts, notify);
    await updatePhase('vault', {});

    // Phase 8
    // Hydrate paper rows for vault notes.
    const candidateIds = (tierData.decisions || []).map(d => d.candidateId);
    let paperRowsMap = new Map();
    if (candidateIds.length) {
      const rcRes = await _query(
        `SELECT rc.candidate_id::text AS candidate_id, p.paper_id::text AS paper_id,
                p.title, p.abstract, p.authors, p.source, p.source_url, p.venue,
                p.published_date::text AS published_date, p.ingested_at::text AS ingested_at
           FROM research_candidates rc
           LEFT JOIN research_corpus p USING (source_url)
          WHERE rc.candidate_id::text = ANY($1::text[])`,
        [candidateIds]
      );
      paperRowsMap = new Map(rcRes.rows.map(r => [r.candidate_id, r]));
    }
    const finalRunRow = {
      run_id:            runId,
      started_at:        new Date().toISOString(),
      status:            opts.dryRun ? 'completed (dry run)' : 'completed',
      sources_discovered: expandData.sourcesDiscovered,
      papers_ingested:   sweepData.ingested,
      papers_rated:      rateData.rated,
      implementable_n:   rateData.implementableN,
      paperhunters_run:  huntData.run,
      tier_a_count:      tierData.tiers.A.length,
      tier_b_count:      tierData.tiers.B.length,
      tier_c_count:      tierData.tiers.C.length,
      coded_synchronous: codeResults.coded,
      coded_failed:      codeResults.failed,
      cost_usd:          totalCost,
    };
    await _closeout(
      { runRow: finalRunRow,
        tierResults: tierData,
        codeResults,
        stageResults,
        manifest: preflightData.manifest,
        paperRows: paperRowsMap },
      opts, notify
    );

    await _query(
      `UPDATE saturday_runs
          SET status='completed', finished_at=NOW(), current_phase='done',
              cost_usd=$2
        WHERE run_id=$1`,
      [runId, totalCost]
    ).catch(() => {});
    notify(`saturday_run ${runId.slice(0,8)} complete — $${totalCost.toFixed(2)}`);

    return {
      run_id: runId,
      costUsd: totalCost,
      sources_discovered: expandData.sourcesDiscovered,
      papers_ingested:    sweepData.ingested,
      papers_rated:       rateData.rated,
      implementable_n:    rateData.implementableN,
      paperhunters_run:   huntData.run,
      tier_counts:        { A: tierData.tiers.A.length, B: tierData.tiers.B.length, C: tierData.tiers.C.length },
      coded_synchronous:  codeResults.coded,
      tier_b_staged:      stageResults.staged,
    };
  } catch (e) {
    notify(`FATAL: ${e.message}`);
    await _query(
      `UPDATE saturday_runs
          SET status='failed', finished_at=NOW(), error_detail=$2,
              cost_usd=$3
        WHERE run_id=$1`,
      [runId, e.message, totalCost]
    ).catch(() => {});
    throw e;
  }
}

module.exports = { run };
