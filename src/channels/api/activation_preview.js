'use strict';

// activation_preview.js — parser for the Strategy Activation assigner's
// --dry-run stdout (src/backtest/activation_assigner.py). Consumed by
// server.js's POST /api/activation/dry-run, which ONLY ever invokes the
// assigner with --dry-run (the live eligibility write is operator-gated,
// Phase 1e). Pure text-in/JSON-out — no DB, no fs, unit-testable
// standalone (tests/test_activation_slider_api.js).
//
// The assigner is line-oriented; every line it owns is prefixed
// `[activation_assigner] ` via its _log helper. The four shapes we parse
// (verbatim from activation_assigner.py main()/_fmt_diff):
//
//   [activation_assigner] threshold=0.5 min_trades=20 dry_run=True strategies=149
//   [activation_assigner]   S_alpha: LOW_VOL: True->False (deactivated), TRANSITIONING: None->False (initialized), HIGH_VOL: False->False, CRISIS: True->True
//   [activation_assigner]   SKIP  S_beta: no primary_window backtest with regime rows
//   [activation_assigner]   ERROR S_gamma: <exception text>
//   [activation_assigner] activation_assigner summary: 145 strategies evaluated, 4 skipped (no corrected backtest), 12 cell(s) activated, 33 cell(s) deactivated, 2 newly-dormant strategies (S_a, S_b), threshold=0.5, min_trades=20, dry_run=True, errors=0
//
// Per-strategy diff bodies come from _fmt_diff iterating CANONICAL_REGIMES
// in fixed order, so a diff body ALWAYS starts with `LOW_VOL: ` — that
// anchors the detail-line discriminator against SKIP/ERROR lines. In each
// cell `prior ∈ {True,False,None}` (None = no strategy_regime_params row
// yet), `new ∈ {True,False}`, and the parenthesised action
// ('activated'|'deactivated'|'initialized') is present only when the cell
// would change; unchanged cells carry no suffix. The trailing
// `strateg{y|ies}` grammar flexes with count and the parenthesised
// newly-dormant list is omitted entirely when empty.

const CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

// Cap on the per-strategy detail rows returned to the browser — the counts
// above the cap stay exact; only the row list is truncated.
const MAX_CHANGED_STRATEGIES = 500;

const HEADER_RE = /^\[activation_assigner\] threshold=([-+\d.eE]+) min_trades=(\d+) dry_run=(\w+) strategies=(\d+)\s*$/;
const DETAIL_RE = /^\[activation_assigner\]\s+(\S+): (LOW_VOL: .+)$/;
const SKIP_RE   = /^\[activation_assigner\]\s+SKIP\s+\S+:/;
const ERROR_RE  = /^\[activation_assigner\]\s+ERROR\s+\S+:/;
const SUMMARY_RE = new RegExp(
  '^\\[activation_assigner\\] activation_assigner summary: (\\d+) strategies evaluated, ' +
  '(\\d+) skipped \\(no corrected backtest\\), (\\d+) cell\\(s\\) activated, ' +
  '(\\d+) cell\\(s\\) deactivated, (\\d+) newly-dormant strateg(?:y|ies)' +
  '(?: \\(([^)]*)\\))?, threshold=([-+\\d.eE]+), min_trades=(\\d+), dry_run=(\\w+), errors=(\\d+)\\s*$'
);
const CELL_RE = /(LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS): (True|False|None)->(True|False)(?: \((\w+)\))?/g;

/**
 * Parse `python3 -m backtest.activation_assigner --all --dry-run` stdout.
 *
 * Returns a structured diff:
 *   summary_found        — the authoritative summary line was present
 *   threshold/min_trades — from the summary (header fallback)
 *   evaluated/skipped/errors
 *   cells                — { activated, deactivated } totals (summary-preferred)
 *   per_regime           — per canonical regime:
 *                            eligible_now      (# prior=True — current DB reality)
 *                            eligible_preview  (# new=True  — hypothetical)
 *                            dormant_preview   (# new=False — hypothetical)
 *                            activated/deactivated/initialized/unchanged action counts
 *   newly_dormant        — strategies with ≥1 prior-eligible regime and ALL 4
 *                          regimes False in the preview (summary list preferred,
 *                          per-line recomputation as fallback/cross-check)
 *   newly_dormant_count
 *   changed_strategies   — [{strategy_id, cells:{REGIME:{before,after,action}}, newly_dormant}]
 *                          for strategies with ≥1 changing cell (capped)
 *   warnings             — parse cross-check discrepancies (never throws)
 */
function parseActivationDryRun(stdout) {
  const perRegime = {};
  for (const r of CANONICAL_REGIMES) {
    perRegime[r] = {
      eligible_now: 0, eligible_preview: 0, dormant_preview: 0,
      activated: 0, deactivated: 0, initialized: 0, unchanged: 0,
    };
  }
  const changed = [];
  const newlyDormantParsed = [];
  let header = null;
  let summary = null;
  let summaryLine = null;
  let detailCount = 0, skipCount = 0, errorCount = 0, changedTruncated = false;

  for (const line of String(stdout || '').split('\n')) {
    let m;
    if ((m = line.match(DETAIL_RE))) {
      detailCount += 1;
      const sid = m[1];
      const cells = {};
      CELL_RE.lastIndex = 0;
      let cm;
      while ((cm = CELL_RE.exec(m[2])) !== null) {
        const regime = cm[1];
        const before = cm[2] === 'None' ? null : cm[2] === 'True';
        const after  = cm[3] === 'True';
        const action = cm[4] || 'unchanged';
        cells[regime] = { before, after, action };
        const pr = perRegime[regime];
        if (before === true) pr.eligible_now += 1;
        if (after) pr.eligible_preview += 1; else pr.dormant_preview += 1;
        if (Object.prototype.hasOwnProperty.call(pr, action)) pr[action] += 1;
      }
      const wasActive = Object.values(cells).some(c => c.before === true);
      const isActive  = Object.values(cells).some(c => c.after === true);
      const goesDormant = wasActive && !isActive;
      if (goesDormant) newlyDormantParsed.push(sid);
      if (Object.values(cells).some(c => c.action !== 'unchanged')) {
        if (changed.length < MAX_CHANGED_STRATEGIES) {
          changed.push({ strategy_id: sid, cells, newly_dormant: goesDormant });
        } else {
          changedTruncated = true;
        }
      }
      continue;
    }
    if (SKIP_RE.test(line))  { skipCount += 1; continue; }
    if (ERROR_RE.test(line)) { errorCount += 1; continue; }
    if ((m = line.match(HEADER_RE))) {
      header = {
        threshold: parseFloat(m[1]), min_trades: parseInt(m[2], 10),
        dry_run: m[3] === 'True', strategies: parseInt(m[4], 10),
      };
      continue;
    }
    if ((m = line.match(SUMMARY_RE))) {
      summaryLine = line.trim();
      summary = {
        evaluated: parseInt(m[1], 10), skipped: parseInt(m[2], 10),
        cells_activated: parseInt(m[3], 10), cells_deactivated: parseInt(m[4], 10),
        newly_dormant_count: parseInt(m[5], 10),
        newly_dormant: m[6] ? m[6].split(',').map(s => s.trim()).filter(Boolean) : [],
        threshold: parseFloat(m[7]), min_trades: parseInt(m[8], 10),
        dry_run: m[9] === 'True', errors: parseInt(m[10], 10),
      };
    }
  }

  // Cross-checks: the per-line recomputation must agree with the summary
  // the assigner printed. Disagreement means the printed format drifted —
  // surface it loudly instead of silently trusting one side.
  const warnings = [];
  const cellsActivated   = CANONICAL_REGIMES.reduce((s, r) => s + perRegime[r].activated, 0);
  const cellsDeactivated = CANONICAL_REGIMES.reduce((s, r) => s + perRegime[r].deactivated, 0);
  if (!summary) {
    warnings.push('assigner summary line not found — counts derived from per-strategy lines only');
  } else {
    if (summary.newly_dormant_count !== newlyDormantParsed.length) {
      warnings.push('newly-dormant mismatch: summary=' + summary.newly_dormant_count +
                    ' recomputed=' + newlyDormantParsed.length);
    }
    if (summary.cells_activated !== cellsActivated || summary.cells_deactivated !== cellsDeactivated) {
      warnings.push('cell-count mismatch: summary=' + summary.cells_activated + '/' + summary.cells_deactivated +
                    ' recomputed=' + cellsActivated + '/' + cellsDeactivated);
    }
    if (summary.evaluated !== detailCount) {
      warnings.push('evaluated mismatch: summary=' + summary.evaluated + ' detail lines=' + detailCount);
    }
  }

  const newlyDormant = (summary && summary.newly_dormant.length)
    ? summary.newly_dormant
    : newlyDormantParsed;

  return {
    summary_found: !!summary,
    threshold:  summary ? summary.threshold  : (header ? header.threshold  : null),
    min_trades: summary ? summary.min_trades : (header ? header.min_trades : null),
    evaluated:  summary ? summary.evaluated  : detailCount,
    skipped:    summary ? summary.skipped    : skipCount,
    errors:     summary ? summary.errors     : errorCount,
    cells: {
      activated:   summary ? summary.cells_activated   : cellsActivated,
      deactivated: summary ? summary.cells_deactivated : cellsDeactivated,
    },
    per_regime: perRegime,
    newly_dormant: newlyDormant,
    newly_dormant_count: summary ? summary.newly_dormant_count : newlyDormantParsed.length,
    changed_strategies: changed,
    changed_strategies_truncated: changedTruncated,
    summary_line: summaryLine,
    warnings,
  };
}

module.exports = { parseActivationDryRun, CANONICAL_REGIMES };
