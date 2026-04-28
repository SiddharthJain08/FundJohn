#!/usr/bin/env node
'use strict';

/**
 * saturday_brain_finisher.js — push paperhunter-extracted specs through the
 * remaining Phase 5–8 of the brain, without re-spawning paperhunter.
 *
 * Use case: a brain (or recovery) run paperhuntered N candidates but
 * crashed/hung before tier filter + code + stage. The hunter_result_json
 * payloads are persisted in research_candidates — this script reads them
 * back and runs:
 *   5. data_tier_filter.tierCandidate
 *   6. _codeFromQueue for Tier A (capped)
 *   7. STAGING manifest write for Tier B
 *   8. deferred vault notes for Tier C
 *
 * Idempotent on strategy_id — skips strategies already in manifest.
 *
 * Usage:
 *   node src/agent/curators/saturday_brain_finisher.js [--since-iso YYYY-MM-DD]
 *                                                      [--strategy-ids ID1,ID2]
 *                                                      [--tier-a-cap N]
 *                                                      [--dry-run]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');

// .env so POSTGRES_URI etc. are populated when run standalone.
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) {}

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
  const dryRun       = !!getArg('--dry-run', false);
  const sinceIsoArg  = getArg('--since-iso');
  const tierACap     = parseInt(getArg('--tier-a-cap', '10'), 10);
  const sidsArg      = getArg('--strategy-ids');
  const log = (m) => console.error(`[finisher] ${m}`);

  log(`Starting finisher (cap=${tierACap}, dryRun=${dryRun})`);

  // Load candidates with extracted hunter specs that aren't yet in manifest.
  const filters = [
    "hunter_result_json->>'strategy_id' IS NOT NULL",
    "hunter_result_json->>'rejection_reason_if_any' IS NULL",
  ];
  const params = [];
  if (sinceIsoArg && sinceIsoArg !== true) {
    params.push(sinceIsoArg);
    filters.push(`submitted_at >= $${params.length}`);
  } else {
    filters.push("submitted_at > NOW() - INTERVAL '48 hours'");
  }
  if (sidsArg && sidsArg !== true) {
    const sids = String(sidsArg).split(',').map(s => s.trim()).filter(Boolean);
    params.push(sids);
    filters.push(`hunter_result_json->>'strategy_id' = ANY($${params.length}::text[])`);
  }

  const { rows: candidates } = await _query(
    `SELECT candidate_id::text AS candidate_id,
            source_url,
            hunter_result_json
       FROM research_candidates
      WHERE ${filters.join(' AND ')}
      ORDER BY submitted_at DESC`,
    params,
  );
  log(`Found ${candidates.length} candidates with extracted hunter specs.`);
  if (candidates.length === 0) { process.exit(0); }

  // Filter out already-manifested strategies (idempotency).
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  manifest.strategies = manifest.strategies || {};
  const fresh = candidates.filter(c => {
    const sid = c.hunter_result_json.strategy_id;
    if (manifest.strategies[sid]) {
      log(`  skip ${sid} — already in manifest as state=${manifest.strategies[sid].state}`);
      return false;
    }
    return true;
  });
  log(`After idempotency check: ${fresh.length} fresh.`);

  if (fresh.length === 0) { process.exit(0); }

  // Phase 5 — data tier filter.
  const capabilityMap = await dataTierFilter.buildCapabilityMap(_query);
  const tiers = { A: [], B: [], C: [] };
  for (const cand of fresh) {
    const decision = dataTierFilter.tierCandidate(cand.hunter_result_json, capabilityMap);
    tiers[decision.tier].push({
      candidateId: cand.candidate_id,
      hunterResult: cand.hunter_result_json,
      sourceUrl: cand.source_url,
      decision,
    });
    if (!dryRun) {
      await _query(
        `UPDATE research_candidates SET data_tier = $1 WHERE candidate_id = $2`,
        [decision.tier, cand.candidate_id]
      ).catch(() => {});
    }
  }
  log(`Tiered: A=${tiers.A.length} B=${tiers.B.length} C=${tiers.C.length}`);
  if (dryRun) {
    for (const t of ['A','B','C']) {
      for (const e of tiers[t]) {
        log(`  [${t}] ${e.hunterResult.strategy_id} — ${(e.hunterResult.hypothesis_one_liner||'').slice(0,80)}`);
      }
    }
    process.exit(0);
  }

  // Phase 6 — Tier-A synchronous code.
  const orch = new ResearchOrchestrator();
  const cap = Math.min(tierACap, tiers.A.length);
  log(`Coding Tier-A: ${cap}/${tiers.A.length}`);
  let coded = 0, failed = 0;
  const tierAStrategies = [];
  for (let i = 0; i < cap; i++) {
    const { hunterResult, candidateId } = tiers.A[i];
    const sid = hunterResult.strategy_id;
    log(`  code [${i+1}/${cap}] ${sid}`);
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
        tierAStrategies.push({ strategy_id: sid, hunterResult });
      } else {
        failed++;
        log(`    not promoted (outcome=${JSON.stringify(outcome).slice(0,200)})`);
      }
    } catch (e) {
      failed++;
      log(`    error: ${e.message}`);
    }
  }
  log(`Coded: ${coded} promoted, ${failed} failed.`);

  // Phase 7 — Tier-B → STAGING (manifest write + strategy_registry upsert).
  // Cross-process locked read-modify-write — see src/lib/manifest_lock.js.
  let staged = 0;
  if (tiers.B.length > 0) {
    const { withManifestLock } = require('../../lib/manifest_lock');
    const now = new Date().toISOString();
    await withManifestLock(MANIFEST_PATH, async (m) => {
      m.strategies = m.strategies || {};
      for (const { hunterResult, candidateId, decision } of tiers.B) {
        const sid = hunterResult.strategy_id;
        if (m.strategies[sid]) continue;
        m.strategies[sid] = {
          state: 'staging', state_since: now,
          metadata: {
            canonical_file: `${sid.toLowerCase()}.py`,
            class:          sid,
            description:    (hunterResult.hypothesis_one_liner || sid).slice(0, 280),
          },
          history: [{
            from_state: null, to_state: 'staging', timestamp: now,
            actor: 'saturday_brain_finisher',
            reason: 'Tier-B from finisher: data fetchable, awaiting operator approval',
            metadata: { decision, candidate_id: candidateId },
          }],
        };
        const { upsertStrategyRegistry } = require('../../lib/strategy_registry_upsert');
        await upsertStrategyRegistry({
          id: sid,
          name: m.strategies[sid].metadata.class,
          implementationPath: `src/strategies/implementations/${m.strategies[sid].metadata.canonical_file}`,
          status: 'pending_approval',
          dataRequirementsPlanned: decision.provider_route || [],
          parameters: { candidate_id: candidateId, hypothesis: hunterResult.hypothesis_one_liner },
          universe: [String(hunterResult.universe || 'SP500').replace(/\s+/g, '')],
          dbQuery: _query,
        }).catch((e) => log(`  stage[${sid}]: registry upsert failed — ${e.message}`));
        staged++;
      }
      m.updated_at = now;
      return m;
    }, { actor: 'saturday_brain_finisher.stage' });
    log(`Staged ${staged} Tier-B candidates.`);
  }

  // Phase 8 — Tier-C deferred vault notes + Tier-A/B vault notes for newly-coded.
  let deferred = 0, paperNotes = 0, strategyNotes = 0;
  for (const { hunterResult, candidateId, decision, sourceUrl } of tiers.C) {
    const { rows: paperRow } = await _query(
      `SELECT * FROM research_corpus WHERE source_url = $1`, [sourceUrl]
    );
    if (!paperRow.length) continue;
    if (vaultLinker.writeDeferredPaperNote(paperRow[0], decision.missing_columns, decision.unlock_provider_estimate)) {
      deferred++;
    }
  }
  // Vault notes for the coded Tier-A strategies.
  const finalManifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  for (const { strategy_id, hunterResult } of tierAStrategies) {
    if (vaultLinker.writeStrategyNote(strategy_id, finalManifest.strategies[strategy_id], hunterResult, [])) {
      strategyNotes++;
    }
  }
  log(`Vault: ${paperNotes} paper, ${deferred} deferred, ${strategyNotes} strategy notes.`);

  console.log(JSON.stringify({
    candidates_processed: fresh.length,
    tier_counts:          { A: tiers.A.length, B: tiers.B.length, C: tiers.C.length },
    coded:                coded,
    coding_failed:        failed,
    staged_tier_b:        staged,
    deferred_tier_c:      deferred,
    new_strategies:       [...tierAStrategies.map(s => s.strategy_id),
                           ...tiers.B.map(t => t.hunterResult.strategy_id)],
  }, null, 2));
  log('Finisher complete.');
}

main().catch((e) => {
  console.error('[finisher] FATAL:', e.message);
  console.error(e.stack);
  process.exit(1);
});
