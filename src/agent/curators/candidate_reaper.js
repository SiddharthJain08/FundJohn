#!/usr/bin/env node
'use strict';

/**
 * candidate_reaper.js — candidate lifecycle terminator (operator directive
 * 2026-07-14): a research candidate gets three weekends of automatic
 * investigation (Sunday code review), alteration (Saturday review/refresh)
 * and gate re-offers (Sunday auto-approval sweep). If it still hasn't earned
 * a live promotion after that, it is EJECTED — completely removed from the
 * operational system:
 *
 *   - manifest entry REMOVED (invisible to engine, dashboard, research)
 *   - implementation .py + .requirements.json DELETED (git history retains)
 *   - strategy_regime_params rows DELETED
 *   - strategy_registry row kept as a TOMBSTONE with status='deprecated' —
 *     lifecycle_events has an FK onto strategy_registry(id), and the
 *     CLAUDE.md doctrine is "deprecation is a flag, never a DELETE"; the
 *     tombstone preserves the full audit trail.
 *   - lifecycle_events audit row (to_state 'ejected', actor
 *     system:candidate-reaper) written while the registry row still exists.
 *
 * DEDUP TRACES ARE PRESERVED: research_candidates rows and
 *  strategy_signatures.json entries are NOT touched — they are what stops
 * the research pipeline from re-minting the same strategy next weekend.
 *
 * Exemption: manifest metadata.reaper_exempt === true (reference strategies
 * that are candidate-only BY DESIGN, e.g. the SP-4 options-lane proof).
 *
 * Rule: state === 'candidate' AND state_since older than --max-age-days
 * (default 21 ≈ three weekend cycles). Runs as Phase 10 of the Sunday
 * finisher, AFTER the auto-approval sweep — a candidate qualifying on its
 * third weekend is promoted before the reaper looks at it.
 *
 * CLI:
 *   node src/agent/curators/candidate_reaper.js [--dry-run] [--max-age-days N] [--limit N]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const IMPL_DIR      = path.join(OPENCLAW_DIR, 'src/strategies/implementations');

// .env so POSTGRES_URI is populated when run standalone.
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) {}

function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 2 });
  return _query._pool.query(sql, params);
}

// Implementation-file candidates for a sid: canonical_file from the manifest
// record, the exact-case default, and the first-char case toggle (the same
// drift the research orchestrator tolerates).
function _implFiles(sid, rec) {
  const names = new Set();
  const cf = rec && rec.metadata && rec.metadata.canonical_file;
  if (cf) names.add(cf);
  names.add(`${sid}.py`);
  for (const n of [...names]) {
    if (n.length > 0) {
      const toggled = n[0] === n[0].toUpperCase()
        ? n[0].toLowerCase() + n.slice(1) : n[0].toUpperCase() + n.slice(1);
      names.add(toggled);
    }
  }
  const out = [];
  for (const n of names) {
    out.push(path.join(IMPL_DIR, n));
    out.push(path.join(IMPL_DIR, n.replace(/\.py$/, '.requirements.json')));
  }
  return out.filter(p => fs.existsSync(p));
}

async function reapCandidates(opts = {}) {
  const log        = opts.log || ((m) => console.error(`[candidate_reaper] ${m}`));
  const dryRun     = !!opts.dryRun;
  const maxAgeDays = opts.maxAgeDays || 21;
  const limit      = opts.limit || Infinity;

  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const strategies = manifest.strategies || {};
  const cutoffMs = Date.now() - maxAgeDays * 24 * 60 * 60 * 1000;

  const targets = [], exempt = [];
  for (const sid of Object.keys(strategies).sort()) {
    const rec = strategies[sid];
    if (rec.state !== 'candidate') continue;
    const t = Date.parse(rec.state_since || '');
    const aged = !Number.isFinite(t) || t < cutoffMs;   // unparseable age fails closed → eject
    if (!aged) continue;
    if (rec.metadata && rec.metadata.reaper_exempt === true) {
      exempt.push(sid);
      continue;
    }
    if (targets.length < limit) targets.push(sid);
  }
  const ageDays = (rec) => {
    const t = Date.parse(rec.state_since || '');
    return Number.isFinite(t) ? Math.floor((Date.now() - t) / 86400000) : null;
  };
  log(`${targets.length} candidate(s) past ${maxAgeDays}d without promotion` +
      (exempt.length ? ` (+${exempt.length} exempt: ${exempt.join(', ')})` : '') +
      (dryRun ? ' [DRY-RUN]' : ''));

  const ejected = [], errors = [];
  for (const sid of targets) {
    const rec = strategies[sid];
    const age = ageDays(rec);
    if (dryRun) { ejected.push(sid); log(`  WOULD EJECT ${sid} (${age}d)`); continue; }
    try {
      // 1. Audit row FIRST (registry row must still exist — FK).
      //    Non-fatal if the strategy never had a registry row (orphan).
      try {
        await _query(
          `INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata)
           VALUES ($1, 'candidate', 'ejected', 'system:candidate-reaper', $2, $3::jsonb)`,
          [sid, `candidate for ${age}d > ${maxAgeDays}d without live promotion — ejected (3-weekend rule)`,
           JSON.stringify({ age_days: age, state_since: rec.state_since || null })]);
      } catch (e) { log(`  ${sid}: lifecycle_events insert skipped (${e.message})`); }
      // 2. Registry tombstone (flag, never DELETE — FK + audit doctrine).
      //    NOTE: strategy_registry has NO updated_at column — use the
      //    dedicated deprecated_at/deprecation_reason columns.
      await _query(
        `UPDATE strategy_registry
            SET status='deprecated', deprecated_at=NOW(),
                deprecation_reason=$2
          WHERE id=$1`,
        [sid, `candidate-reaper: ${age}d in candidate without live promotion (3-weekend rule)`]
      ).catch(e => log(`  ${sid}: registry tombstone skipped (${e.message})`));
      // 3. Sizer params gone.
      await _query(`DELETE FROM strategy_regime_params WHERE strategy_id=$1`, [sid]);
      // 4. Implementation files gone (git history retains them).
      for (const f of _implFiles(sid, rec)) {
        try { fs.unlinkSync(f); } catch (e) { log(`  ${sid}: unlink ${path.basename(f)} failed (${e.message})`); }
      }
      ejected.push(sid);
      log(`  EJECTED ${sid} (${age}d)`);
    } catch (e) {
      errors.push({ sid, error: e.message });
      log(`  ${sid}: ERROR ${e.message} — manifest entry left in place for retry`);
    }
  }

  // 5. Manifest entries removed in ONE locked write (only fully-ejected sids).
  if (!dryRun && ejected.length > 0) {
    const { withManifestLock } = require('../../lib/manifest_lock');
    await withManifestLock(MANIFEST_PATH, (m) => {
      for (const sid of ejected) delete (m.strategies || {})[sid];
      m.updated_at = new Date().toISOString();
      return m;
    }, { actor: 'system:candidate-reaper' });
    log(`manifest: ${ejected.length} entr${ejected.length === 1 ? 'y' : 'ies'} removed`);
  }

  const summary = { ejected, exempt, errors, dry_run: dryRun, max_age_days: maxAgeDays };
  log(`done: ${ejected.length} ejected, ${exempt.length} exempt, ${errors.length} errors`);
  return summary;
}

module.exports = { reapCandidates };

if (require.main === module) {
  const getArg = (name, fallback = null) => {
    const i = process.argv.indexOf(name);
    if (i < 0) return fallback;
    const next = process.argv[i + 1];
    if (!next || next.startsWith('--')) return true;
    return next;
  };
  reapCandidates({
    dryRun:     !!getArg('--dry-run', false),
    maxAgeDays: parseInt(getArg('--max-age-days', '21'), 10),
    limit:      getArg('--limit') ? parseInt(getArg('--limit'), 10) : undefined,
  }).then((s) => {
    console.log(JSON.stringify(s, null, 2));
    process.exit(s.errors.length ? 1 : 0);
  }).catch((e) => {
    console.error('[candidate_reaper] FATAL:', e.message);
    process.exit(1);
  });
}
