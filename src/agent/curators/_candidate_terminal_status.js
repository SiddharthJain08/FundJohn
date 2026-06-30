'use strict';

// Pure: map a finisher per-candidate outcome to the terminal research_candidates.status the
// scheduled pipeline should stamp (the pipeline historically never wrote status -> the column
// lied; W4-2). Uses ONLY the existing status state-machine values. Returns null when no
// terminal stamp applies (e.g. Tier-A coding failed -> leave 'pending' for a future retry).
//
// `rejected` (hunter_result_json.rejection_reason_if_any present) takes precedence over `tier`,
// mirroring the historical backfill's CASE precedence. Note: the `rejected` branch is dead code
// in BOTH live lanes — the finisher's candidate query filters `rejection_reason_if_any IS NULL`
// before selecting rows, and the recovery's tiering loop skips any row where
// r.rejection_reason_if_any is truthy — so no rejected row ever reaches this helper in either
// lane. The branch exists solely to mirror the one-time backfill's CASE precedence for textual
// parity. The common no-provider Tier-C case (no rejection, no data) maps to 'blocked_unclassified'.
function terminalStatusFor({ tier, promoted, rejected }) {
  if (rejected) return 'blocked_rejected';
  if (tier === 'A') return promoted ? 'done' : null;  // failed coding -> null (leave pending for retry)
  if (tier === 'B') return 'blocked_buildable';
  if (tier === 'C') return 'blocked_unclassified';
  return null;
}

module.exports = { terminalStatusFor };
