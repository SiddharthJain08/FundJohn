'use strict';

// activation_preview.js — parser for the Strategy Activation assigner's
// --dry-run stdout (src/backtest/activation_assigner.py). Consumed by
// server.js's POST /api/activation/dry-run, which ONLY ever invokes the
// assigner with --dry-run (the live eligibility write is operator-gated,
// Phase 1e). Pure text-in/JSON-out — no DB, no fs, unit-testable
// standalone (tests/test_activation_api.test.js).
//
// Eligibility is bench-relative as of 2026-09-25 (spec docs/specs/2026-
// 09-25-activation-bench-relative-spec.md) — the min-Sharpe SLIDER this
// file used to help drive is retired; the `threshold=` token below is now
// display-only (the S_beta_spy LOW_VOL comparator), kept byte-stable
// because this parser (and the dashboard fields it feeds) still reads it.
// The min-TRADES slider is unchanged.
//
// The assigner is line-oriented; every line it owns is prefixed
// `[activation_assigner] ` via its _log helper. The seven shapes we parse
// (verbatim from activation_assigner.py main()/_fmt_diff/_fmt_bench_vector/
// _fmt_bench_diff/_log):
//
//   [activation_assigner] threshold=0.5 min_trades=20 dry_run=True strategies=149
//   [activation_assigner]   S_alpha: LOW_VOL: True->False (deactivated), TRANSITIONING: None->False (initialized), HIGH_VOL: False->False, CRISIS: True->True
//   [activation_assigner]   SKIP  S_beta: no primary_window backtest with regime rows
//   [activation_assigner]   ERROR S_gamma: <exception text>
//   [activation_assigner] activation_assigner summary: 145 strategies evaluated, 4 skipped (no corrected backtest), 12 cell(s) activated, 33 cell(s) deactivated, 2 newly-dormant strategies (S_a, S_b), threshold=0.5, min_trades=20, dry_run=True, errors=0
//   [activation_assigner] bench: sleeve=S_beta_spy source=registry run=r51b5b915 LOW_VOL=0.95 TRANSITIONING=0.44 HIGH_VOL=0.53 CRISIS=1.58
//   [activation_assigner] bench diff: LOW_VOL +4/-1 TRANSITIONING +7/-0 HIGH_VOL +12/-0 CRISIS +0/-14
//   [activation_assigner] WARN: CRISIS bench sharpe missing from sleeve run r1; using last-applied pipeline_config vector
//
// The WARN: shape (F1-a, fix round 1) is emitted whenever a regime's bench
// comparator degrades to the tier-2 pipeline_config fallback or the tier-3
// DEFAULT_MIN_SHARPE (spec §2) -- previously these lines fell straight
// through this parser (no branch matched them) and were invisible to the
// operator anywhere on the dashboard; every one is now collected into
// `warnings` below, alongside the parser's own cross-check discrepancies.
// Per-strategy diff bodies come from _fmt_diff iterating CANONICAL_REGIMES
// in fixed order, so a diff body ALWAYS starts with `LOW_VOL: ` — that
// anchors the detail-line discriminator against SKIP/ERROR lines. In each
// cell `prior ∈ {True,False,None}` (None = no strategy_regime_params row
// yet), `new ∈ {True,False}`, and the parenthesised action
// ('activated'|'deactivated'|'initialized') is present only when the cell
// would change; unchanged cells carry no suffix. The trailing
// `strateg{y|ies}` grammar flexes with count and the parenthesised
// newly-dormant list is omitted entirely when empty.
//
// The bench: / bench diff: lines (spec §5-E, Task 1) are the REAL
// comparator preview — the actual per-regime S_beta_spy Sharpe vector this
// dry-run judged every strategy against, and the prior->new gained/lost
// tally per regime. Deliberately shaped to never match HEADER_RE/DETAIL_RE/
// SUMMARY_RE (no `<token>: LOW_VOL: ` shape, no leading `threshold=`), so
// they fell through this parser silently before Task 2 wired them up here.

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
// bench: sleeve=S_beta_spy source=registry run=r51b5b915 LOW_VOL=0.95 TRANSITIONING=0.44 HIGH_VOL=0.53 CRISIS=1.58
const BENCH_VECTOR_RE = new RegExp(
  '^\\[activation_assigner\\] bench: sleeve=(\\S+) source=(\\S+) run=(\\S+) ' +
  'LOW_VOL=([-+\\d.eE]+) TRANSITIONING=([-+\\d.eE]+) HIGH_VOL=([-+\\d.eE]+) CRISIS=([-+\\d.eE]+)\\s*$'
);
// bench diff: LOW_VOL +4/-1 TRANSITIONING +7/-0 HIGH_VOL +12/-0 CRISIS +0/-14
const BENCH_DIFF_RE = new RegExp(
  '^\\[activation_assigner\\] bench diff: LOW_VOL \\+(\\d+)/-(\\d+) TRANSITIONING \\+(\\d+)/-(\\d+) ' +
  'HIGH_VOL \\+(\\d+)/-(\\d+) CRISIS \\+(\\d+)/-(\\d+)\\s*$'
);
const CELL_RE = /(LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS): (True|False|None)->(True|False)(?: \((\w+)\))?/g;
// [activation_assigner] WARN: CRISIS bench sharpe missing from sleeve run r1; using last-applied pipeline_config vector
// (F1-a, fix round 1) -- degraded bench fallback (tier-2 pipeline_config /
// tier-3 DEFAULT_MIN_SHARPE), a multi-sleeve registry, or a non-finite
// sleeve sharpe. Captured verbatim (message only, prefix stripped) into
// `warnings` so a degraded comparator is never invisible to the operator.
const WARN_RE = /^\[activation_assigner\] WARN: (.+)$/;

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
 *   warnings             — assigner WARN: lines (degraded bench fallback,
 *                          F1-a fix round 1), THEN parse cross-check
 *                          discrepancies (never throws)
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
  const warnLines = [];   // F1-a: assigner WARN: lines, collected verbatim
  let header = null;
  let summary = null;
  let summaryLine = null;
  let bench = null;
  let benchDiff = null;
  let detailCount = 0, skipCount = 0, errorCount = 0, changedTruncated = false;

  for (const line of String(stdout || '').split('\n')) {
    let m;
    if ((m = line.match(WARN_RE))) { warnLines.push(m[1].trim()); continue; }
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
      continue;
    }
    // spec §5-E (Task 1 output, Task 2 wiring): the REAL per-regime
    // comparator this dry-run judged every strategy against, and the
    // prior->new gained/lost tally per regime — the operator's actual ack
    // material, since `threshold=` above is a single display number.
    if ((m = line.match(BENCH_VECTOR_RE))) {
      bench = {
        sleeve_id: m[1], sleeve_source: m[2], run_id: m[3] === 'None' ? null : m[3],
        values: {
          LOW_VOL: parseFloat(m[4]), TRANSITIONING: parseFloat(m[5]),
          HIGH_VOL: parseFloat(m[6]), CRISIS: parseFloat(m[7]),
        },
      };
      continue;
    }
    if ((m = line.match(BENCH_DIFF_RE))) {
      benchDiff = {
        LOW_VOL:       { gained: parseInt(m[1], 10), lost: parseInt(m[2], 10) },
        TRANSITIONING: { gained: parseInt(m[3], 10), lost: parseInt(m[4], 10) },
        HIGH_VOL:      { gained: parseInt(m[5], 10), lost: parseInt(m[6], 10) },
        CRISIS:        { gained: parseInt(m[7], 10), lost: parseInt(m[8], 10) },
      };
    }
  }

  // Cross-checks: the per-line recomputation must agree with the summary
  // the assigner printed. Disagreement means the printed format drifted —
  // surface it loudly instead of silently trusting one side.
  const warnings = [...warnLines];   // F1-a: assigner WARN: lines first
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
    // spec §5-E: the actual bench-relative comparator this dry-run used
    // (`bench.values`, sleeve id/source/run) and its prior->new gained/lost
    // tally per regime (`bench_diff`). null when the assigner's stdout
    // didn't carry a bench: line (e.g. a hard failure before that point).
    bench,
    bench_diff: benchDiff,
    warnings,
  };
}

// ── Bench-relative activation card (Task 2, spec 2026-09-25-activation-
// bench-relative §3) ─────────────────────────────────────────────────────
// Pure helpers for GET/PUT /api/config/activation-min-sharpe, factored out
// here for the same reason parseActivationDryRun is here: testable without
// booting server.js or touching a DB (mirrors the module's own docstring
// above). server.js imports these and wires them onto `app`.
const ACTIVATION_BENCH_HYSTERESIS_DEFAULT = 0.10;   // mirrors activation_assigner.ACTIVATION_HYSTERESIS

/**
 * Pure GET /api/config/activation-min-sharpe response builder: the
 * S_beta_spy per-regime Sharpe vector + hysteresis band actually in force,
 * read-only. No DB access, no Express — server.js issues ONE query
 * (`pipeline_config WHERE key = ANY($1)` over [strategy_activation_
 * last_applied, strategy_activation_bench_sharpe]) and hands both rows here.
 *
 * markerRow / benchRow: {value, updated_at} | null|undefined — `value` is
 * the raw pipeline_config text (JSON), exactly what dbQuery's row shape
 * carries; either may be absent (row doesn't exist).
 *
 * Tier order: marker.bench_sharpe (Task 1, written on every successful
 * --all apply) -> the tier-2 fallback row (spec §2, for a marker stamped
 * before Task 1 ever wrote bench_sharpe) -> {bench: null, source: null}
 * (the assigner hasn't run since bench-relative activation shipped, or
 * both rows are malformed).
 */
function benchCardPayload(markerRow, benchRow, defaultBand) {
  const band = defaultBand != null ? defaultBand : ACTIVATION_BENCH_HYSTERESIS_DEFAULT;
  let marker = null;
  if (markerRow && markerRow.value != null) {
    try { marker = JSON.parse(markerRow.value); } catch (_) { marker = null; }
  }
  let bench = (marker && marker.bench_sharpe) ? marker.bench_sharpe : null;
  let source = bench ? 'last_applied' : null;
  // F1-c (fix round 1): per-regime provenance for THIS bench vector, from
  // stamp_last_applied's bench_regime_source (Task 1 review item, wired up
  // in this fix round). Only meaningful when `source === 'last_applied'`
  // -- the tier-2 fallback row below has no per-regime provenance of its
  // own (the MARKER predates the bench rule entirely, see source ===
  // 'pipeline_config_fallback' below) and an older last_applied marker
  // (stamped before this fix landed) never carried the field either.
  // Both cases leave regimeSource null -- NEVER guessed at, so a regime
  // with unknown provenance gets no note rather than a wrong one.
  let regimeSource = (bench && marker && marker.bench_regime_source
                      && typeof marker.bench_regime_source === 'object')
    ? marker.bench_regime_source : null;
  if (!bench && benchRow && benchRow.value != null) {
    try {
      const parsed = JSON.parse(benchRow.value);
      if (parsed && typeof parsed === 'object') { bench = parsed; source = 'pipeline_config_fallback'; }
    } catch (_) { /* leave bench null — malformed tier-2 row */ }
  }
  // Per-regime display note for a degraded (non-sleeve) regime this apply:
  // 'fallback' (this regime's number came from the tier-2 pipeline_config
  // vector, not a live S_beta_spy backtest) or 'DEFAULT 0.50 — WARN' (no
  // sleeve run AND no tier-2 vector -- DEFAULT_MIN_SHARPE, and a WARN was
  // logged). A regime absent from bench_notes is sleeve-sourced, or its
  // provenance is unknown (older marker) -- no note either way, never
  // claimed as good or bad without evidence.
  const bench_notes = {};
  if (bench && regimeSource) {
    for (const r of Object.keys(bench)) {
      const src = regimeSource[r];
      if (src === 'pipeline_config') bench_notes[r] = 'fallback';
      else if (src === 'default') bench_notes[r] = 'DEFAULT 0.50 — WARN';
    }
  }
  return {
    bench,
    bench_notes,
    regime_source: regimeSource,
    hysteresis:   (marker && marker.bench_hysteresis != null) ? marker.bench_hysteresis : band,
    bench_run_id: marker ? (marker.bench_run_id || null) : null,
    source,
    applied_at:   markerRow ? (markerRow.updated_at != null ? markerRow.updated_at : null) : null,
    row_exists:   !!markerRow,
  };
}

// PUT /api/config/activation-min-sharpe: REMOVED (Task 2). 410 Gone (the
// resource existed and was intentionally, permanently retired) — not 404
// (this isn't "never existed" or "wrong path").
const MIN_SHARPE_GONE_BODY = {
  error: 'the activation min-Sharpe slider was removed 2026-09-25 (spec docs/specs/2026-09-25-activation-bench-relative-spec.md) — '
       + 'eligibility is bench-relative (S_beta_spy per-regime Sharpe) now; there is nothing left to set here. '
       + 'GET this same path for the read-only comparator actually in force.',
};
function minSharpeGone(req, res) {
  res.status(410).json(MIN_SHARPE_GONE_BODY);
}

module.exports = {
  parseActivationDryRun, CANONICAL_REGIMES,
  benchCardPayload, minSharpeGone, MIN_SHARPE_GONE_BODY,
  ACTIVATION_BENCH_HYSTERESIS_DEFAULT,
};
