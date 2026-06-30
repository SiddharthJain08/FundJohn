'use strict';
/**
 * _candidate_dedup.js — shared fingerprint-dedup helper for the Tier-A coding
 * loop.  Used by saturday_brain_finisher.js AND saturday_brain_recovery.js so
 * the two lanes can't diverge.
 *
 * Fail-OPEN contract: any error / timeout / missing-fingerprint → return false
 * (proceed to code), never block the finisher or recovery.
 *
 * CLI invoked: python3 src/research/fingerprint_dedup.py \
 *   --slug <id> --tokens <a,b,c> --regimes <x,y or 'any'>
 * Prints JSON { duplicate: bool, reason, matches, threshold } and exits 0.
 */

const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');

/**
 * Returns true iff fingerprint_dedup.py reports duplicate:true for this
 * candidate.  Always fails open (returns false) on any error or missing
 * fingerprint, so the caller's coding lane is never blocked.
 *
 * @param {string}   sid         - strategy_id slug
 * @param {object}   hunterResult - raw hunter_result_json
 * @param {function} log         - log(message) callback (e.g. console.error)
 * @param {object}   [_inject]   - optional { execFileSync } override for tests
 * @returns {boolean}
 */
function _isDuplicateCandidate(sid, hunterResult, log, _inject) {
  try {
    const sf = hunterResult && hunterResult.similarity_fingerprint;
    const tokens = (sf && Array.isArray(sf.formula_tokens)) ? sf.formula_tokens.join(',') : '';
    if (!tokens) return false; // no fingerprint → fail-open, let it code
    const regimes = (Array.isArray(hunterResult.regime_applicability) && hunterResult.regime_applicability.length)
      ? hunterResult.regime_applicability.join(',') : 'any';
    const execFileSync = (_inject && _inject.execFileSync) || require('child_process').execFileSync;
    const out = execFileSync(
      'python3',
      ['src/research/fingerprint_dedup.py', '--slug', sid, '--tokens', tokens, '--regimes', regimes],
      { cwd: OPENCLAW_DIR, timeout: 30000, encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'src' } }
    );
    const res = JSON.parse(out);
    if (res && res.duplicate) {
      log(`  dedup-skip ${sid}: ${res.reason || 'fingerprint match'}`);
      return true;
    }
    return false;
  } catch (e) {
    log(`  dedup check failed for ${sid} (fail-open, will code): ${e.message}`);
    return false;
  }
}

module.exports = { _isDuplicateCandidate };
