#!/usr/bin/env node
'use strict';

/**
 * saturday_brain_recovery.js — one-shot recovery for the 2026-04-25 first
 * live run.
 *
 * The first live brain run used IMPL_BUCKET_THRESH=0.65, which Opus turned
 * out to be pessimistic against on the all-time historical corpus
 * (only ~1% of academic papers cleared 0.65). The threshold has been
 * lowered to 0.40 in mastermind.js for next week, but THIS week's data is
 * already rated under the old logic. This script rescues it.
 *
 * Steps:
 *   1. Find curated_candidates rows for the most recent corpus run with
 *      implementability_score ≥ 0.40 that haven't been promoted yet.
 *   2. Re-bucket them to 'implementable_candidate'.
 *   3. Insert/promote them into research_candidates.
 *   4. Spawn paperhunter fan-out via runHunterFanout (concurrency 8).
 *   5. Apply data_tier_filter to the hunter results.
 *   6. For Tier-A candidates: synchronously code via _codeFromQueue.
 *   7. For Tier-B candidates: push to STAGING (operator-gated, like the
 *      main brain).
 *   8. For Tier-C candidates: write deferred vault notes.
 *   9. Print a summary + post Discord update.
 *
 * Idempotent: skips rows already promoted (queued_candidate_id IS NOT NULL).
 *
 * Usage: node src/agent/curators/saturday_brain_recovery.js [--dry-run]
 *                                                         [--threshold 0.40]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');

// Load .env so POSTGRES_URI etc. are populated when run standalone.
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) {}

const MastermindCurator    = require('./mastermind');
const dataTierFilter       = require('./data_tier_filter');
const vaultLinker          = require('./vault_linker');
const ResearchOrchestrator = require('../research/research-orchestrator');

function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  const next = process.argv[i + 1];
  if (!next || next.startsWith('--')) return true;
  return next;
}

async function main() {
  const dryRun = !!getArg('--dry-run', false);
  const threshold = parseFloat(getArg('--threshold', '0.40'));
  const runIdArg = getArg('--run-id');
  const log = (m) => console.error(`[recovery] ${m}`);

  log(`Starting saturday brain recovery (threshold=${threshold}, dryRun=${dryRun})`);

  // ── 1+2. Find + re-bucket high-impl rows. By default uses the most recent
  // corpus run; pass --run-id <UUID> to target an older paused/abandoned run.
  let runId;
  if (runIdArg && runIdArg !== true) {
    runId = String(runIdArg);
    const { rows: check } = await _query(
      `SELECT 1 FROM curator_runs WHERE run_id::text = $1`, [runId]
    );
    if (!check.length) {
      log(`--run-id ${runId.slice(0, 8)} does not exist. Aborting.`);
      process.exit(2);
    }
  } else {
    const { rows: runRow } = await _query(
      `SELECT run_id::text AS run_id FROM curator_runs
        WHERE status IN ('completed','partial','running')
        ORDER BY started_at DESC LIMIT 1`
    );
    if (!runRow.length) {
      log('No curator_runs found — nothing to recover.');
      process.exit(0);
    }
    runId = runRow[0].run_id;
  }
  log(`Targeting curator run: ${runId.slice(0, 8)}`);

  const { rows: highImpl } = await _query(
    `SELECT cc.candidate_eval_id, cc.paper_id::text AS paper_id, cc.confidence,
            cc.implementability_score, cc.predicted_bucket, p.source_url
       FROM curated_candidates cc
       JOIN research_corpus p USING (paper_id)
      WHERE cc.run_id = $1
        AND cc.implementability_score >= $2
        AND cc.queued_candidate_id IS NULL
        AND NOT EXISTS (
          SELECT 1 FROM research_candidates rc WHERE rc.source_url = p.source_url
        )
      ORDER BY cc.implementability_score DESC, cc.confidence DESC`,
    [runId, threshold]
  );
  log(`High-impl unpromoted rows: ${highImpl.length}`);
  if (highImpl.length === 0) {
    log('Nothing to recover. Exit 0.');
    process.exit(0);
  }
  if (dryRun) {
    log('DRY RUN — would re-bucket + promote these rows. Exiting.');
    for (const row of highImpl.slice(0, 10)) {
      log(`  impl=${row.implementability_score} conf=${row.confidence} ${row.source_url.slice(0, 60)}`);
    }
    process.exit(0);
  }

  // Re-bucket in place so promoteHighBucket picks them up.
  await _query(
    `UPDATE curated_candidates
        SET predicted_bucket = 'implementable_candidate'
      WHERE candidate_eval_id = ANY($1::uuid[])`,
    [highImpl.map(r => r.candidate_eval_id)]
  );
  log(`Re-bucketed ${highImpl.length} rows to implementable_candidate.`);

  // ── 3. Promote via the existing helper.
  const curator = new MastermindCurator();
  const promotion = await curator.promoteHighBucket({ runId, maxToPromote: 600 });
  log(`promoteHighBucket: promoted=${promotion.promoted} eligible=${promotion.eligible}`);

  // Hydrate the candidate IDs we just inserted.
  const { rows: candidates } = await _query(
    `SELECT rc.candidate_id::text AS candidate_id, p.title, p.published_date::text AS published_date
       FROM research_candidates rc
       JOIN research_corpus p USING (source_url)
       JOIN curated_candidates cc ON cc.paper_id = p.paper_id AND cc.run_id = $1
      WHERE cc.implementability_score >= $2
        AND rc.submitted_by = 'curator'
        AND (rc.hunter_result_json IS NULL OR rc.hunter_result_json::text IN ('null','{}'))
      ORDER BY rc.priority DESC, rc.submitted_at DESC`,
    [runId, threshold]
  );
  log(`Candidates ready for paperhunter fan-out: ${candidates.length}`);
  if (candidates.length === 0) {
    log('Nothing to fan out. Exit 0.');
    process.exit(0);
  }

  // ── 4. Paperhunter fan-out.
  const orch = new ResearchOrchestrator();
  const ids = candidates.map(c => c.candidate_id);
  log(`Spawning paperhunter on ${ids.length} candidates (concurrency 8)...`);
  const hunterResults = await orch.runHunterFanout(ids, {
    concurrency: 8,
    notify: (m) => log(`  hunt: ${m}`),
  });

  // ── 5. Data-tier filter.
  const capabilityMap = await dataTierFilter.buildCapabilityMap(_query);
  const tiers = { A: [], B: [], C: [] };
  for (const r of hunterResults) {
    if (!r || !r.candidate_id || r.rejection_reason_if_any) continue;
    const decision = dataTierFilter.tierCandidate(r, capabilityMap);
    tiers[decision.tier].push({ candidateId: r.candidate_id, hunterResult: r, decision });
    await _query(
      `UPDATE research_candidates SET data_tier = $1 WHERE candidate_id = $2`,
      [decision.tier, r.candidate_id]
    ).catch(() => {});
  }
  log(`Tiered: A=${tiers.A.length} B=${tiers.B.length} C=${tiers.C.length}`);

  // ── 6. Tier-A synchronous coding (cap at 30 to avoid stomping on the
  //      main brain's Phase 6 if it's still running).
  const TIER_A_CAP = parseInt(getArg('--tier-a-cap', '30'), 10);
  const cap = Math.min(TIER_A_CAP, tiers.A.length);
  log(`Coding Tier-A: ${cap}/${tiers.A.length} (cap=${TIER_A_CAP})`);
  let coded = 0, failed = 0;
  const tierAStrategies = [];
  for (let i = 0; i < cap; i++) {
    const { hunterResult, candidateId } = tiers.A[i];
    const sid = hunterResult.strategy_id;
    if (!sid) { failed++; continue; }
    log(`  code [${i + 1}/${cap}] ${sid}`);
    try {
      const item = {
        candidate_id: candidateId,
        strategy_spec: { ...hunterResult, strategy_id: sid, candidate_id: candidateId },
      };
      const outcome = await orch._codeFromQueue(item, undefined, undefined, {
        onPhase: (phase, pct) => log(`    ${phase} ${pct}%`),
      });
      if (outcome && outcome.promoted) {
        coded++;
        tierAStrategies.push(sid);
      } else {
        failed++;
      }
    } catch (e) {
      log(`    error: ${e.message}`);
      failed++;
    }
  }
  log(`Tier-A coding complete: ${coded} promoted to PAPER, ${failed} failed.`);

  // ── 7. Tier-B → STAGING (mirrors saturday_brain.js _stage).
  // Cross-process locked read-modify-write — see src/lib/manifest_lock.js.
  let staged = 0;
  if (tiers.B.length > 0) {
    const { withManifestLock } = require('../../lib/manifest_lock');
    const now = new Date().toISOString();
    await withManifestLock(MANIFEST_PATH, async (manifest) => {
      manifest.strategies = manifest.strategies || {};
      for (const { hunterResult, candidateId, decision } of tiers.B) {
        const sid = hunterResult.strategy_id;
        if (!sid) continue;
        if (!manifest.strategies[sid]) {
          manifest.strategies[sid] = {
            state: 'staging', state_since: now,
            metadata: {
              canonical_file: `${sid.toLowerCase()}.py`,
              class:          sid,
              description:    (hunterResult.hypothesis_one_liner || sid).slice(0, 280),
            },
            history: [{ from_state: null, to_state: 'staging', timestamp: now,
                        actor: 'saturday_brain_recovery',
                        reason: 'Tier-B from recovery: data fetchable, awaiting operator approval',
                        metadata: { decision, candidate_id: candidateId } }],
          };
        }
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
        }).catch((e) => log(`  stage[${sid}]: registry upsert failed — ${e.message}`));
        staged++;
      }
      manifest.updated_at = now;
      return manifest;
    }, { actor: 'saturday_brain_recovery.stage' });
    log(`Staged ${staged} Tier-B candidates.`);
  }

  // ── 8. Tier-C deferred vault notes.
  let deferred = 0;
  for (const { hunterResult, candidateId, decision } of tiers.C) {
    const { rows: paperRow } = await _query(
      `SELECT p.* FROM research_corpus p
         JOIN research_candidates rc ON rc.source_url = p.source_url
        WHERE rc.candidate_id::text = $1`,
      [candidateId]
    );
    if (!paperRow.length) continue;
    const noteFile = vaultLinker.writeDeferredPaperNote(
      paperRow[0], decision.missing_columns, decision.unlock_provider_estimate,
    );
    if (noteFile) deferred++;
  }
  log(`Wrote ${deferred} Tier-C deferred vault notes.`);

  // ── 9. Summary + Discord post.
  const summary = {
    recovered_candidates: candidates.length,
    paperhunters_run:     hunterResults.length,
    tier_counts:          { A: tiers.A.length, B: tiers.B.length, C: tiers.C.length },
    coded_synchronous:    coded,
    coding_failed:        failed,
    tier_b_staged:        staged,
    tier_c_deferred:      deferred,
    threshold_used:       threshold,
  };
  console.log(JSON.stringify(summary, null, 2));
  log('Recovery complete.');
}

main().catch((e) => {
  console.error('[recovery] FATAL:', e.message);
  console.error(e.stack);
  process.exit(1);
});
