'use strict';

/**
 * mastermind.js — MastermindJohn (Opus 4.7, 1M context).
 *
 * Runs in two modes; each has its own systemd timer:
 *   mode=corpus          (Sat 10:00 ET) — surveys research_corpus in batches,
 *                        emits calibrated pass/reject predictions into
 *                        curated_candidates, promotes high-bucket papers.
 *   mode=strategy-stack  (Fri 20:00 ET) — reads strategy_stats,
 *                        signal_performance, alpaca_submissions,
 *                        position_recommendations, veto_log; writes a memo
 *                        to #strategy-memos and sizing recs to
 *                        #position-recommendations; persisted in
 *                        mastermind_weekly_reports (migration 047).
 *
 * This file owns the corpus mode; strategy-stack lives in
 * ./strategy_stack.js so the two flows stay independently testable.
 *
 * Design points (corpus mode):
 *   - Batch size: start at 100 papers/batch. Retune from actual token usage.
 *   - Threshold: confidence ≥ 0.75 → 'high' bucket → queued into research_candidates.
 *   - Hard cap: 600 high-bucket promotions per run. Alerts if hit.
 *   - Dry-run: runs against a supplied paper-id list, skips DB writes, emits a
 *     calibration report (precision/recall vs. paper_gate_decisions history).
 *
 * Public API (corpus mode):
 *   const c = new MastermindCurator();
 *   await c.run({ dryRun: false, batchSize: 100, paperIds: null });
 *   await c.promoteHighBucket({ runId, maxToPromote: 600 });
 *   await c.calibrationReport(runId);
 *   await c.getStatus();
 *
 * Was previously corpus_curator.js / class CorpusCurator. Renamed in
 * Phase 3 of the 10am-cycle pipeline restructure.
 */

const fs   = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { emitGateDecision } = require('../research/gate-decisions');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const NODE_CLI     = path.join(OPENCLAW_DIR, 'src/agent/run-subagent-cli.js');
const WORKSPACE    = path.join(OPENCLAW_DIR, 'workspaces/default');

const DEFAULT_BATCH_SIZE      = 100;
const HIGH_CONFIDENCE_THRESH  = 0.75;
const HARD_PROMOTE_CAP        = 600;

// Phase 3: spot-check sampling. A small fraction of non-high-bucket papers are
// promoted to research_candidates alongside the high bucket so we can observe
// downstream outcomes and detect false negatives. Without this, a curator that
// mistakenly rejects viable papers has no feedback to correct the bias.
const DEFAULT_SPOT_CHECK_MAX  = 10;   // per run
const SPOT_CHECK_WEIGHTS      = { med: 3, low: 1, reject: 0 };   // sampling weights

// Saturday brain (2026-04-25): pre-filter papers whose abstracts lack any
// blueprint-signal terms before sending them to Opus. Drops ~30–50% of pure
// theory / survey / non-strategy papers without spending Opus tokens on them.
// Keep this regex permissive — false positives are cheap (Opus filters them
// anyway) but false negatives are expensive (we'd skip a real strategy).
const BLUEPRINT_SIGNAL_RX = new RegExp(
  '\\b(' +
  'factor|signal|portfolio|momentum|reversal|long-short|long\\/short|' +
  'backtest|sharpe|alpha|hedge|formula|rule|strategy|cross[- ]sectional|' +
  'time[- ]series|carry|pairs|ranking|decile|quintile|kelly|optimal|' +
  'mean[- ]variance|risk[- ]parity|trend|breakout|reversion|gamma|' +
  'volatility|skew|term[- ]structure' +
  ')\\b',
  'i'
);

// New bucket added by the saturday brain: 'implementable_candidate'. Indicates
// implementability_score ≥ 0.40 — direction is clear and StrategyCoder can
// fill in heuristic gaps. The data-tier filter (Phase 5) takes care of "do
// we have the data" downstream; gating here on confidence-based pessimism
// would silently drop concrete recipes that Opus thinks won't backtest
// well — that's exactly the call the operator wants paperhunter + the
// actual backtest to make, not the rater. The operator's directive is
// implementability-first, "find 100+ blueprints per run".
//
// Threshold calibration (2026-04-25): set initially at 0.65, observed
// distribution on the all-time historical corpus showed only ~1% of
// papers clear 0.65 (Opus is pessimistic on academic theory papers).
// Lowering to 0.40 yields ~8% of corpus — extrapolates to ~110 candidates
// per all-time run, ~50 per incremental week. Matches the operator's
// "100+ blueprints" target. StrategyCoder + paperhunter handle the gap
// between "paper says do X" and "Python file does X" — they're the
// mechanism the operator chose for filling heuristic recipes.
//
// Old high/med/low/reject buckets still tracked alongside.
const IMPL_BUCKET_THRESH = 0.40;   // implementability_score floor
const ALL_BUCKETS = ['high', 'med', 'low', 'reject', 'implementable_candidate'];

class MastermindCurator {
  constructor() {
    this._pool = null;
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Run a curation pass.
   * @param {Object} opts
   * @param {boolean} [opts.dryRun=false]   — No DB writes to curated_candidates or research_candidates.
   * @param {number}  [opts.batchSize=100]  — Papers per subagent invocation.
   * @param {string[]} [opts.paperIds]      — Curate only these paper_ids (Array of UUIDs). If null, pulls un-curated papers.
   * @param {Function} [opts.notify]        — Optional progress callback (string arg).
   * @returns {Object} { runId, inputCount, outputCount, buckets, costUsd }
   */
  async run({ dryRun = false, batchSize = DEFAULT_BATCH_SIZE, paperIds = null, notify } = {}) {
    const log = (m) => { notify?.(m); console.error(`[curator] ${m}`); };

    const papers = await this._fetchInput(paperIds);
    log(`Loaded ${papers.length} papers to curate${dryRun ? ' (dry run)' : ''}.`);
    if (papers.length === 0) {
      return { runId: null, inputCount: 0, outputCount: 0, buckets: {}, costUsd: 0 };
    }

    // Register curator_runs row (skipped on dry-run).
    let runId = null;
    if (!dryRun) {
      const { rows } = await this._query(
        `INSERT INTO curator_runs (model, input_count, status)
         VALUES ($1, $2, 'running') RETURNING run_id`,
        ['claude-opus-4-7', papers.length]
      );
      runId = rows[0].run_id;
      log(`curator_run ${runId.slice(0, 8)} started.`);
    }

    // Build cacheable context once per run.
    const cacheableCtx = await this._buildCacheableContext();
    log(`Cacheable context: ${Math.round(JSON.stringify(cacheableCtx).length / 1024)}KB.`);

    // Chunk into batches.
    const batches = [];
    for (let i = 0; i < papers.length; i += batchSize) {
      batches.push(papers.slice(i, i + batchSize));
    }
    log(`Split into ${batches.length} batch(es) of ~${batchSize}.`);

    // Run batches sequentially (prompt-cache stays warm across back-to-back calls).
    // Persist per-batch so a mid-run failure doesn't lose completed work.
    const allResults = [];
    let totalCost = 0;
    const buckets = { high: 0, med: 0, low: 0, reject: 0, implementable_candidate: 0 };

    const MAX_BATCH_RETRIES = 2;

    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      log(`Batch ${i + 1}/${batches.length} — judging ${batch.length} papers...`);
      let batchResult;
      let lastErr;
      for (let attempt = 0; attempt <= MAX_BATCH_RETRIES; attempt++) {
        try {
          batchResult = await this._runBatch(batch, cacheableCtx, { batchIdx: i + 1, total: batches.length });
          break;
        } catch (e) {
          lastErr = e;
          log(`Batch ${i + 1} attempt ${attempt + 1} failed: ${e.message.slice(0, 180)}`);
          if (attempt < MAX_BATCH_RETRIES) {
            const waitMs = 30_000 * (attempt + 1);   // 30s, 60s backoff
            log(`retrying in ${waitMs / 1000}s...`);
            await new Promise(r => setTimeout(r, waitMs));
          }
        }
      }
      if (!batchResult) {
        log(`Batch ${i + 1} exhausted retries — stopping run. Previously-persisted batches are retained.`);
        if (!dryRun) {
          await this._query(
            `UPDATE curator_runs SET status = 'partial', finished_at = NOW(),
                 output_count = $1, total_cost_usd = $2,
                 run_metadata = COALESCE(run_metadata, '{}'::jsonb) ||
                                jsonb_build_object('error', $3::text, 'failed_batch', $4::int,
                                                   'buckets', $5::jsonb, 'batches_completed', $6::int)
             WHERE run_id = $7`,
            [
              allResults.length, totalCost,
              (lastErr?.message || 'unknown').slice(0, 500),
              i + 1, JSON.stringify(buckets), i, runId,
            ]
          );
        }
        throw lastErr || new Error('batch failed');
      }
      totalCost += batchResult.costUsd;
      for (const r of batchResult.ratings) {
        allResults.push(r);
        if (buckets[r.predicted_bucket] != null) buckets[r.predicted_bucket] += 1;
      }
      // Persist this batch immediately — prevents loss if a subsequent batch fails.
      if (!dryRun) {
        await this._persistRatings(runId, batchResult.ratings);
      }
      log(`Batch ${i + 1} done — ${batchResult.ratings.length} ratings, $${batchResult.costUsd.toFixed(4)} (persisted).`);
    }

    // Finalize run.
    if (!dryRun) {
      await this._query(
        `UPDATE curator_runs
             SET finished_at = NOW(),
                 output_count = $1,
                 total_cost_usd = $2,
                 status = 'completed',
                 run_metadata = COALESCE(run_metadata, '{}'::jsonb) ||
                                jsonb_build_object('buckets', $3::jsonb, 'batches', $4::int)
           WHERE run_id = $5`,
        [allResults.length, totalCost, JSON.stringify(buckets), batches.length, runId]
      );
    }

    log(`Run complete: ${allResults.length} rated, $${totalCost.toFixed(4)}, buckets=${JSON.stringify(buckets)}.`);
    return { runId, inputCount: papers.length, outputCount: allResults.length, buckets, costUsd: totalCost, ratings: allResults };
  }

  /**
   * Promote high-bucket curated_candidates into research_candidates. Idempotent
   * via the research_candidates.source_url unique-style check + the
   * curated_candidates.queued_candidate_id backlink.
   *
   * Also promotes a small random spot-check sample from the med/low buckets
   * (weighted by `SPOT_CHECK_WEIGHTS`) so false-negative detection works —
   * without this the calibration loop is unidirectional. See advisor review.
   *
   * @param {Object} opts
   * @param {string} opts.runId             — Which curator run to promote from.
   * @param {number} [opts.maxToPromote=600]
   * @param {number} [opts.spotCheckMax=10] — Cap on random spot-check promotions.
   * @returns {Object} { promoted, capped, eligible, spotChecked }
   */
  async promoteHighBucket({ runId, maxToPromote = HARD_PROMOTE_CAP, spotCheckMax = DEFAULT_SPOT_CHECK_MAX } = {}) {
    if (!runId) throw new Error('promoteHighBucket: runId required');

    // ── High-bucket promotion ───────────────────────────────────────────────
    // Saturday brain extension: 'implementable_candidate' is treated as a
    // high-tier promotion class. Priority for that bucket factors in
    // implementability_score (so the most concrete recipes go first into the
    // Phase-4 paperhunter fan-out).
    const { rows: picks } = await this._query(
      `SELECT cc.candidate_eval_id, cc.paper_id, cc.confidence,
              cc.implementability_score, cc.predicted_bucket, p.source_url
         FROM curated_candidates cc
         JOIN research_corpus p USING (paper_id)
        WHERE cc.run_id = $1
          AND cc.predicted_bucket IN ('high','implementable_candidate')
          AND cc.queued_candidate_id IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM research_candidates rc
             WHERE rc.source_url = p.source_url
          )
        ORDER BY
          (CASE WHEN cc.predicted_bucket = 'implementable_candidate' THEN 1 ELSE 0 END) DESC,
          (COALESCE(cc.implementability_score, 0) * cc.confidence) DESC,
          cc.confidence DESC
        LIMIT $2`,
      [runId, maxToPromote]
    );

    let promoted = 0;
    for (const row of picks) {
      // Saturday brain priority blends confidence × implementability so the
      // research_candidates queue (read by paperhunter fan-out) ranks
      // implementable papers ahead of merely-high-confidence ones.
      const blended = (row.predicted_bucket === 'implementable_candidate' && row.implementability_score != null)
        ? Math.round(row.confidence * Number(row.implementability_score) * 10)
        : Math.round(row.confidence * 10);
      const { rows: inserted } = await this._query(
        `INSERT INTO research_candidates (source_url, submitted_by, priority, status)
         VALUES ($1, 'curator', $2, 'pending')
         ON CONFLICT DO NOTHING
         RETURNING candidate_id`,
        [row.source_url, Math.max(1, blended)]
      );
      if (inserted.length) {
        await this._query(
          `UPDATE curated_candidates
              SET queued_candidate_id = $1
            WHERE candidate_eval_id = $2`,
          [inserted[0].candidate_id, row.candidate_eval_id]
        );
        promoted += 1;
      }
    }
    const capped = picks.length === maxToPromote;

    // ── Spot-check random sample from med/low buckets ──────────────────────
    const spotChecked = await this._promoteSpotCheckSample(runId, spotCheckMax);

    return { promoted, capped, eligible: picks.length, spotChecked };
  }

  /**
   * Promote a weighted random sample of med/low bucket papers with
   * submitted_by='curator_spotcheck'. Returns the number promoted.
   */
  async _promoteSpotCheckSample(runId, maxToPromote) {
    if (!maxToPromote || maxToPromote <= 0) return 0;

    // Only sample from buckets with non-zero weight; otherwise a small run
    // (fewer eligible candidates than the limit) ends up promoting everything.
    const eligibleBuckets = Object.entries(SPOT_CHECK_WEIGHTS)
      .filter(([, w]) => w > 0)
      .map(([b]) => b);
    if (!eligibleBuckets.length) return 0;

    const whenClauses = Object.entries(SPOT_CHECK_WEIGHTS)
      .filter(([, w]) => w > 0)
      .map(([bucket, w]) => `WHEN '${bucket}' THEN ${w}`)
      .join(' ');

    const { rows: picks } = await this._query(
      `SELECT cc.candidate_eval_id, cc.paper_id, cc.confidence, cc.predicted_bucket,
              p.source_url
         FROM curated_candidates cc
         JOIN research_corpus p USING (paper_id)
        WHERE cc.run_id = $1
          AND cc.predicted_bucket = ANY($2::text[])
          AND cc.queued_candidate_id IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM research_candidates rc WHERE rc.source_url = p.source_url
          )
        ORDER BY random() * (CASE cc.predicted_bucket ${whenClauses} ELSE 1 END) DESC
        LIMIT $3`,
      [runId, eligibleBuckets, maxToPromote]
    );

    let spotChecked = 0;
    for (const row of picks) {
      const { rows: inserted } = await this._query(
        `INSERT INTO research_candidates (source_url, submitted_by, priority, status)
         VALUES ($1, 'curator_spotcheck', 1, 'pending')
         ON CONFLICT DO NOTHING
         RETURNING candidate_id`,
        [row.source_url]
      );
      if (inserted.length) {
        await this._query(
          `UPDATE curated_candidates
              SET queued_candidate_id = $1
            WHERE candidate_eval_id = $2`,
          [inserted[0].candidate_id, row.candidate_eval_id]
        );
        spotChecked += 1;
      }
    }
    return spotChecked;
  }

  /**
   * Dry-run calibration: for a given runId (or implicit dry-run results), cross
   * reference curator predictions to known outcomes in paper_gate_decisions.
   * Returns precision/recall for the 'high' bucket.
   */
  async calibrationReport(runId, dryRunRatings = null) {
    const ratings = dryRunRatings || (await this._query(
      `SELECT paper_id, confidence, predicted_bucket
         FROM curated_candidates WHERE run_id = $1`,
      [runId]
    )).rows;
    if (!ratings.length) return { error: 'no ratings' };

    const paperIds = [...new Set(ratings.map(r => r.paper_id))];
    const { rows: truths } = await this._query(
      `SELECT paper_id,
              BOOL_OR(gate_name = 'promotion' AND outcome = 'pass') AS promoted,
              BOOL_OR(gate_name = 'convergence' AND outcome = 'pass') AS backtest_pass,
              BOOL_OR(gate_name = 'paperhunter' AND outcome = 'reject') AS hunter_rejected
         FROM paper_gate_decisions
        WHERE paper_id = ANY($1)
        GROUP BY paper_id`,
      [paperIds]
    );
    const truthByPaper = Object.fromEntries(truths.map(t => [t.paper_id, t]));

    let highPromoted = 0, highTotal = 0;
    let promotedTotal = 0, promotedHit = 0;
    const confusions = { high_pass: 0, high_fail: 0, low_pass_miss: 0, low_fail_correct: 0 };

    for (const r of ratings) {
      const t = truthByPaper[r.paper_id];
      if (!t) continue;
      const isHigh     = r.predicted_bucket === 'high';
      const didPromote = !!t.promoted;

      if (didPromote) {
        promotedTotal += 1;
        if (isHigh) promotedHit += 1;
      }
      if (isHigh) {
        highTotal += 1;
        if (didPromote) {
          highPromoted += 1;
          confusions.high_pass += 1;
        } else {
          confusions.high_fail += 1;
        }
      } else {
        if (didPromote) confusions.low_pass_miss += 1;
        else            confusions.low_fail_correct += 1;
      }
    }

    const precision = highTotal    > 0 ? highPromoted / highTotal    : null;
    const recall    = promotedTotal > 0 ? promotedHit  / promotedTotal : null;
    return {
      n_rated_with_truth: Object.keys(truthByPaper).length,
      n_high:       highTotal,
      n_promoted_in_truth: promotedTotal,
      precision_at_high:   precision,
      recall_of_promoted:  recall,
      confusions,
      ship_gate_passed:    (precision ?? 0) >= 0.7 && (recall ?? 0) >= 0.6,
    };
  }

  /**
   * Phase 5a: re-curate previously-rejected papers after data coverage changes.
   *
   * Finds papers whose most-recent curation included a specific failure mode in
   * `predicted_failure_modes`, runs them through the curator again with fresh
   * coverage context, and reports bucket transitions. Makes data-provider
   * investments immediately testable — add a column, then:
   *   c.reCurateByFailureMode({ failureMode: 'data_unavailable:short_interest' })
   *
   * @param {Object} opts
   * @param {string} opts.failureMode    — Target failure mode string. Exact match (element of array).
   * @param {number} [opts.maxPapers]    — Safety cap.
   * @param {boolean} [opts.dryRun]      — Re-rate but don't persist / promote.
   * @param {number}  [opts.batchSize=50]
   * @returns {Object} { runId, transitions: { 'reject→high': N, ...}, ratings }
   */
  async reCurateByFailureMode({ failureMode, maxPapers = 200, dryRun = false, batchSize = 50, notify } = {}) {
    if (!failureMode) throw new Error('reCurateByFailureMode: failureMode required');

    // Pull the latest evaluation per paper that had this failure mode.
    const { rows: targets } = await this._query(
      `WITH latest AS (
         SELECT DISTINCT ON (paper_id)
                paper_id, predicted_bucket, confidence,
                predicted_failure_modes, created_at
           FROM curated_candidates
         ORDER BY paper_id, created_at DESC
       )
       SELECT latest.paper_id, latest.predicted_bucket AS prev_bucket,
              latest.confidence   AS prev_confidence
         FROM latest
        WHERE $1 = ANY(latest.predicted_failure_modes)
        ORDER BY latest.created_at DESC
        LIMIT $2`,
      [failureMode, maxPapers]
    );

    if (targets.length === 0) {
      return { runId: null, transitions: {}, inputCount: 0, note: 'no papers matched' };
    }

    const paperIds = targets.map(t => t.paper_id);
    const prevByPaper = Object.fromEntries(
      targets.map(t => [t.paper_id, { prev_bucket: t.prev_bucket, prev_confidence: Number(t.prev_confidence) }])
    );

    notify?.(`re-curating ${paperIds.length} paper(s) with failure_mode=${failureMode}...`);

    const result = await this.run({ dryRun, batchSize, paperIds, notify });

    // Aggregate transitions.
    const transitions = {};
    const flips = [];   // papers that moved from non-high to high
    for (const r of result.ratings) {
      const prev = prevByPaper[r.paper_id] || {};
      const key = `${prev.prev_bucket}→${r.predicted_bucket}`;
      transitions[key] = (transitions[key] || 0) + 1;
      if (prev.prev_bucket !== 'high' && r.predicted_bucket === 'high') {
        flips.push({
          paper_id:        r.paper_id,
          prev_bucket:     prev.prev_bucket,
          prev_confidence: prev.prev_confidence,
          new_confidence:  r.confidence,
          reasoning:       r.reasoning,
        });
      }
    }

    return {
      runId:       result.runId,
      failureMode,
      inputCount:  result.inputCount,
      costUsd:     result.costUsd,
      transitions,
      unlocked_to_high: flips,
      buckets:     result.buckets,
    };
  }

  async getStatus() {
    const { rows } = await this._query(
      `SELECT run_id, started_at, finished_at, model, input_count, output_count,
              total_cost_usd, status, run_metadata
         FROM curator_runs
        ORDER BY started_at DESC LIMIT 5`
    );
    return rows;
  }

  async sampleRecentDecisions(n = 10) {
    const { rows } = await this._query(
      `SELECT cc.confidence, cc.predicted_bucket, cc.reasoning, cc.predicted_failure_modes,
              p.title, p.source_url
         FROM curated_candidates cc
         JOIN research_corpus p USING (paper_id)
        ORDER BY cc.created_at DESC LIMIT $1`,
      [n]
    );
    return rows;
  }

  // ── Internal ───────────────────────────────────────────────────────────────

  /**
   * Fetch the input paper set. Explicit paperIds override the default behavior
   * of pulling all papers that have NOT been curated in any completed run.
   */
  async _fetchInput(paperIds) {
    if (paperIds && paperIds.length) {
      const { rows } = await this._query(
        `SELECT paper_id, source, source_url, title, abstract, authors, venue,
                published_date, keywords
           FROM research_corpus
          WHERE paper_id = ANY($1)`,
        [paperIds]
      );
      return rows;
    }
    const { rows } = await this._query(
      `SELECT p.paper_id, p.source, p.source_url, p.title, p.abstract, p.authors,
              p.venue, p.published_date, p.keywords
         FROM research_corpus p
         WHERE NOT EXISTS (
           SELECT 1 FROM curated_candidates cc
            JOIN curator_runs cr USING (run_id)
            WHERE cc.paper_id = p.paper_id AND cr.status = 'completed'
         )
         AND LENGTH(COALESCE(p.abstract, '')) >= 50
         ORDER BY p.ingested_at DESC`
    );
    // Saturday-brain pre-filter: skip abstracts that lack any blueprint-signal
    // term. Cheap regex on the title+abstract; saves Opus tokens on pure
    // theory/survey/non-strategy papers without changing the rated set's
    // downstream meaning. Bypassed when paperIds was passed (caller-driven
    // re-curation should rate exactly what was asked, no filtering).
    const filtered = rows.filter(r => {
      const blob = `${r.title || ''} ${r.abstract || ''}`;
      return BLUEPRINT_SIGNAL_RX.test(blob);
    });
    return filtered;
  }

  /** Pull the live coverage + manifest + server columns for the curator prompt. */
  async _buildCacheableContext() {
    // Data ledger: what's actually ingested
    let coverage = [];
    try {
      const { rows } = await this._query(
        `SELECT column_name, provider, min_date, max_date, row_count, ticker_count
           FROM data_columns ORDER BY column_name`
      );
      coverage = rows;
    } catch { /* tolerate empty */ }

    // Server-provided columns (from servers.json)
    let serverColumns = [];
    try {
      const cfg = JSON.parse(fs.readFileSync(
        path.join(OPENCLAW_DIR, 'src/agent/config/servers.json'), 'utf8'
      ));
      for (const s of (cfg.servers || [])) {
        for (const col of (s.covered_columns || [])) {
          serverColumns.push({ column: col, provider: s.name, tier: s.tier });
        }
      }
    } catch { /* tolerate missing */ }

    // Manifest strategies — brief for duplicate detection
    let manifestSummary = [];
    try {
      const manifest = JSON.parse(fs.readFileSync(
        path.join(OPENCLAW_DIR, 'src/strategies/manifest.json'), 'utf8'
      ));
      const strats = manifest.strategies || manifest;
      for (const [id, s] of Object.entries(strats)) {
        manifestSummary.push({
          id,
          state: s.state,
          description: s.metadata?.description || s.description || '',
        });
      }
    } catch { /* tolerate missing */ }

    // Phase 3: calibration feedback + recent miss examples.
    const calibration = await this._loadCalibrationFeedback();

    return { coverage, serverColumns, manifestSummary, calibration };
  }

  /**
   * Load bucket pass-rates and 5 rotating miss examples for the prompt.
   * Feeds the curator its own empirical performance so it stays calibrated
   * instead of drifting over time.
   */
  async _loadCalibrationFeedback() {
    let buckets = [];
    try {
      const { rows } = await this._query(
        `SELECT predicted_bucket, n_rated, n_with_truth,
                n_promoted, n_backtest_pass, promotion_rate
           FROM curator_bucket_calibration`
      );
      buckets = rows;
    } catch { /* view may not exist on first run */ }

    let falsePositives = [];
    try {
      const { rows } = await this._query(
        `SELECT title, confidence, reasoning, predicted_failure_modes,
                hunter_rejected, backtest_failed
           FROM curator_false_positives
          WHERE created_at >= NOW() - INTERVAL '60 days'
          ORDER BY created_at DESC
          LIMIT 5`
      );
      falsePositives = rows;
    } catch { /* ignore */ }

    let falseNegatives = [];
    try {
      const { rows } = await this._query(
        `SELECT title, confidence, predicted_bucket, reasoning, predicted_failure_modes
           FROM curator_false_negatives
          WHERE created_at >= NOW() - INTERVAL '60 days'
          ORDER BY created_at DESC
          LIMIT 5`
      );
      falseNegatives = rows;
    } catch { /* ignore */ }

    // Phase 5b: per-strategy-type priors. Surfaces which categories the curator
    // over- or under-estimates, so e.g. it learns "my ML-based high picks never
    // pass backtest — be more cautious".
    let strategyTypes = [];
    try {
      const { rows } = await this._query(
        `SELECT strategy_type, n_rated, n_with_truth, n_promoted,
                n_backtest_pass, n_hunter_rejected,
                promotion_rate, avg_confidence, high_bucket_fraction
           FROM strategy_type_calibration
          WHERE n_with_truth >= 5
          ORDER BY n_rated DESC
          LIMIT 15`
      );
      strategyTypes = rows;
    } catch { /* view may not exist on first run */ }

    // Phase 5c: per-gate calibration. Tells the model "your paperhunter
    // predictions are well-calibrated but your convergence predictions are
    // systematically 0.2 too optimistic", etc.
    let gateCalibration = [];
    try {
      const { rows } = await this._query(
        `SELECT gate_name, n_observed, avg_predicted, actual_pass_rate, over_confidence_bias
           FROM curator_gate_calibration
          WHERE n_observed >= 5
          ORDER BY gate_name`
      );
      gateCalibration = rows;
    } catch { /* view may not exist yet */ }

    // R4: weekly trend snapshots — lets the model see whether its
    // calibration is improving or drifting over time, not just where it
    // stands right now.
    let trend = [];
    try {
      const { rows } = await this._query(
        `SELECT dimension, key, snapshot_date, promotion_rate,
                actual_pass_rate, over_confidence_bias, n_rated
           FROM curator_priors_trend
          WHERE dimension = 'bucket' AND key = 'high'
             OR dimension = 'gate'
          ORDER BY dimension, key, snapshot_date DESC`
      );
      trend = rows;
    } catch { /* snapshot table may be empty on first run */ }

    return { buckets, falsePositives, falseNegatives, strategyTypes, gateCalibration, trend };
  }

  /**
   * Run one batch: spawn claude-bin via run-subagent-cli.js with a context file
   * carrying the cacheable prefix + the batch of papers.
   */
  async _runBatch(papers, cacheableCtx, batchInfo) {
    const ctx = {
      role:                 'curate_batch',
      DATA_COVERAGE:        JSON.stringify(cacheableCtx.coverage, null, 2),
      SERVER_COLUMNS:       JSON.stringify(cacheableCtx.serverColumns, null, 2),
      MANIFEST_SUMMARY:     JSON.stringify(cacheableCtx.manifestSummary, null, 2),
      CALIBRATION_FEEDBACK: this._renderCalibration(cacheableCtx.calibration),
      PAPER_BATCH:          JSON.stringify(papers.map(this._paperForPrompt), null, 2),
      BATCH_INFO:           JSON.stringify({ index: batchInfo.batchIdx, total: batchInfo.total, size: papers.length }),
    };
    const raw = await this._spawnSubagent('mastermind', `batch-${batchInfo.batchIdx}`, ctx);
    const parsed = this._parseBatchResponse(raw, papers);
    return parsed;
  }

  /** Render calibration feedback as a prompt-friendly string. Empty on cold start. */
  _renderCalibration(cal) {
    if (!cal || (!cal.buckets?.length && !cal.falsePositives?.length && !cal.falseNegatives?.length)) {
      return 'No calibration data yet (first curator run). Apply the rubric strictly and be calibrated.';
    }
    const parts = [];

    if (cal.buckets?.length) {
      parts.push('Your recent empirical performance (promotion_rate = fraction of rated papers that made it to PAPER state):');
      // Hide low-sample buckets so a single stray promotion doesn't teach
      // the curator to loosen globally. 20 is the floor for a meaningful rate.
      const MIN_TRUTH = 20;
      for (const b of cal.buckets) {
        const enoughSample = (b.n_with_truth ?? 0) >= MIN_TRUTH;
        const rate = !enoughSample ? 'n/a (thin sample)' : Number(b.promotion_rate ?? 0).toFixed(3);
        parts.push(`  • ${b.predicted_bucket.padEnd(7)} — ${b.n_rated} rated, ${b.n_with_truth} with truth, ${b.n_promoted} promoted, rate=${rate}`);
      }
      parts.push('');
      parts.push('Aim for high-bucket promotion_rate ≥ 0.70. If your high-bucket rate is trending below 0.5, tighten — move borderline papers to med. If med-bucket rate > 0.3 while high-rate is low, loosen — promote confident med papers to high. Ignore "thin sample" buckets until they accumulate enough observations.');
    }

    if (cal.falsePositives?.length) {
      parts.push('');
      parts.push('Recent FALSE POSITIVES (you rated "high" but the paper was rejected downstream — learn from these):');
      for (const fp of cal.falsePositives.slice(0, 5)) {
        const stage = fp.hunter_rejected ? 'PaperHunter' : fp.backtest_failed ? 'backtest convergence' : 'downstream';
        parts.push(`  • [${Number(fp.confidence).toFixed(2)}] "${(fp.title || '').slice(0, 90)}"`);
        parts.push(`      You said: ${(fp.reasoning || '').slice(0, 160)}`);
        parts.push(`      Actually failed at: ${stage}`);
      }
    }

    if (cal.falseNegatives?.length) {
      parts.push('');
      parts.push('Recent FALSE NEGATIVES (you rated "low/reject" but the paper actually promoted — be less pessimistic here):');
      for (const fn of cal.falseNegatives.slice(0, 5)) {
        parts.push(`  • [${Number(fn.confidence).toFixed(2)} ${fn.predicted_bucket}] "${(fn.title || '').slice(0, 90)}"`);
        parts.push(`      You said: ${(fn.reasoning || '').slice(0, 160)}`);
      }
    }

    if (cal.strategyTypes?.length) {
      parts.push('');
      parts.push('Per-strategy-type empirical rates (only types with ≥5 downstream observations shown):');
      parts.push('  type                      n_rated  truth  promoted  rate   avg_conf  high_frac');
      for (const s of cal.strategyTypes) {
        const rate = s.promotion_rate == null ? ' n/a' : Number(s.promotion_rate).toFixed(3);
        const conf = s.avg_confidence == null ? ' n/a' : Number(s.avg_confidence).toFixed(3);
        const hf   = s.high_bucket_fraction == null ? ' n/a' : Number(s.high_bucket_fraction).toFixed(3);
        parts.push(
          `  ${String(s.strategy_type).padEnd(25)} ${String(s.n_rated).padStart(5)}  ` +
          `${String(s.n_with_truth).padStart(5)}  ${String(s.n_promoted).padStart(5)}   ${rate}     ${conf}     ${hf}`
        );
      }
      parts.push('');
      parts.push('Use these rates as priors when rating a paper of each strategy type. If a category has consistently low promotion_rate despite high avg_conf, your confidence there has been systematically too high.');
    }

    if (cal.gateCalibration?.length) {
      parts.push('');
      parts.push('Per-gate calibration (predicted pass_prob vs actual pass rate; positive bias = overconfident):');
      parts.push('  gate          n_obs  avg_predicted  actual_rate  bias');
      for (const g of cal.gateCalibration) {
        const ap = g.avg_predicted == null ? ' n/a' : Number(g.avg_predicted).toFixed(3);
        const ar = g.actual_pass_rate == null ? ' n/a' : Number(g.actual_pass_rate).toFixed(3);
        const bias = g.over_confidence_bias == null ? '  n/a' : Number(g.over_confidence_bias).toFixed(3);
        parts.push(`  ${String(g.gate_name).padEnd(13)} ${String(g.n_observed).padStart(5)}  ${ap}        ${ar}      ${bias}`);
      }
      parts.push('');
      parts.push('If bias is strongly positive for a gate, lower your pass_prob for that gate on similar papers. Negative bias means you are being too pessimistic.');
    }

    // R4: week-over-week trend lines. Gives the curator a sense of whether
    // its calibration is improving over time, not just where it stands now.
    if (cal.trend?.length) {
      const bucketTrend = cal.trend.filter(t => t.dimension === 'bucket' && t.key === 'high');
      const gateTrend   = cal.trend.filter(t => t.dimension === 'gate');
      if (bucketTrend.length > 1) {
        parts.push('');
        parts.push('High-bucket promotion_rate over time (oldest→newest):');
        const series = bucketTrend.slice().reverse().map(t =>
          `${t.snapshot_date.toISOString().slice(5,10)}=${t.promotion_rate != null ? Number(t.promotion_rate).toFixed(2) : 'n/a'}`
        );
        parts.push('  ' + series.join('  '));
      }
      if (gateTrend.length > 0) {
        const byGate = {};
        for (const t of gateTrend) {
          byGate[t.key] = byGate[t.key] || [];
          byGate[t.key].push(t);
        }
        const multi = Object.entries(byGate).filter(([, v]) => v.length > 1);
        if (multi.length) {
          parts.push('');
          parts.push('Per-gate over-confidence bias over time (oldest→newest; 0 = calibrated, + = overconfident):');
          for (const [g, series] of multi) {
            const line = series.slice().reverse().map(t =>
              `${t.snapshot_date.toISOString().slice(5,10)}=${t.over_confidence_bias != null ? Number(t.over_confidence_bias).toFixed(2) : 'n/a'}`
            ).join('  ');
            parts.push(`  ${g.padEnd(13)} ${line}`);
          }
        }
      }
    }

    return parts.join('\n');
  }

  /** Project a corpus row to the minimal fields the curator needs. */
  _paperForPrompt(p) {
    return {
      paper_id:       p.paper_id,
      source:         p.source,
      title:          p.title,
      abstract:       (p.abstract || '').slice(0, 4000),  // clip extreme outliers
      authors:        p.authors || [],
      venue:          p.venue,
      published_date: p.published_date,
    };
  }

  /**
   * Parse the subagent output. claude-bin returns JSON:
   *   { result: "<string or object>", total_cost_usd: number, ... }
   * `result` may be: our JSON array, or a string containing it (possibly with
   * surrounding whitespace/markdown).
   */
  _parseBatchResponse(raw, papers) {
    let payload = raw;
    if (typeof raw === 'string') {
      try { payload = JSON.parse(raw); } catch { payload = { result: raw }; }
    }
    const costUsd = payload?.total_cost_usd ?? 0;
    let inner = payload?.result ?? payload;

    // Inner may be an object already (model outputted JSON directly), or a string.
    let arr = null;
    if (Array.isArray(inner)) {
      arr = inner;
    } else if (typeof inner === 'string') {
      // Strip code fences if present.
      const cleaned = inner.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
      try { arr = JSON.parse(cleaned); } catch { /* try extract */ }
      if (!Array.isArray(arr)) {
        const m = cleaned.match(/\[[\s\S]*\]/);
        if (m) { try { arr = JSON.parse(m[0]); } catch { /* ignore */ } }
      }
    } else if (inner && Array.isArray(inner.ratings)) {
      arr = inner.ratings;
    }

    if (!Array.isArray(arr)) {
      throw new Error(`curator returned non-array response (type=${typeof inner})`);
    }

    // Sanity-check: align paper_ids, fill in defaults for any missing entries.
    const byId = Object.fromEntries(arr.map(r => [r.paper_id, r]));
    const ratings = papers.map((p) => {
      const r = byId[p.paper_id];
      if (r && typeof r === 'object') {
        // Phase 5c: normalise gate_predictions. Fall back to gracefully-populated
        // defaults for older-format outputs (pass_prob = confidence, same reason).
        const gp = r.gate_predictions && typeof r.gate_predictions === 'object'
          ? r.gate_predictions
          : null;
        const rawConf = Number(r.confidence) || 0;
        // If the model returned gate_predictions but not a matching product,
        // prefer what it wrote for confidence; if it wrote only confidence,
        // synthesise flat gate predictions.
        const gatePredictions = gp || {
          paperhunter:  { pass_prob: Math.cbrt(Math.max(rawConf, 0.001)), reason: 'synthesised from legacy confidence' },
          researchjohn: { pass_prob: Math.cbrt(Math.max(rawConf, 0.001)), reason: 'synthesised from legacy confidence' },
          convergence:  { pass_prob: Math.cbrt(Math.max(rawConf, 0.001)), reason: 'synthesised from legacy confidence' },
        };
        // Saturday-brain implementability_score (0..1). Opus may emit it
        // directly; if not present, derive a conservative default from
        // confidence so downstream tier-promotion still has a usable axis.
        const rawImpl = (r.implementability_score != null)
          ? Math.max(0, Math.min(1, Number(r.implementability_score)))
          : Math.max(0, Math.min(1, rawConf * 0.7));
        // Saturday-brain bucket promotion: 'implementable_candidate' wins
        // when implementability crosses the floor — confidence is NOT
        // gating here. The data-tier filter + paperhunter downstream make
        // the "is this actually viable" call; the rater's job is just to
        // identify recipes concrete enough to be coded. The downstream
        // promoteHighBucket call promotes both 'high' and
        // 'implementable_candidate' labels.
        let bucket = r.predicted_bucket || this._bucketFromConfidence(rawConf);
        if (rawImpl >= IMPL_BUCKET_THRESH) {
          bucket = 'implementable_candidate';
        }
        return {
          paper_id:                 p.paper_id,
          confidence:               rawConf,
          implementability_score:   rawImpl,
          data_requirements_hint:   (r.data_requirements_hint && typeof r.data_requirements_hint === 'object')
                                        ? r.data_requirements_hint : null,
          predicted_bucket:         bucket,
          reasoning:                String(r.reasoning || '').slice(0, 2000),
          predicted_failure_modes:  Array.isArray(r.predicted_failure_modes) ? r.predicted_failure_modes : [],
          gate_predictions:         gatePredictions,
        };
      }
      // Missing from output — treat as low-confidence reject so nothing downstream processes it.
      return {
        paper_id:                p.paper_id,
        confidence:              0,
        implementability_score:  0,
        data_requirements_hint:  null,
        predicted_bucket:        'reject',
        reasoning:               'Curator did not return an entry for this paper.',
        predicted_failure_modes: ['curator_no_response'],
        gate_predictions:        {
          paperhunter:  { pass_prob: 0, reason: 'no response' },
          researchjohn: { pass_prob: 0, reason: 'no response' },
          convergence:  { pass_prob: 0, reason: 'no response' },
        },
      };
    });

    return { ratings, costUsd };
  }

  _bucketFromConfidence(c) {
    if (c >= 0.75) return 'high';
    if (c >= 0.50) return 'med';
    if (c >= 0.25) return 'low';
    return 'reject';
  }

  async _persistRatings(runId, ratings) {
    for (const r of ratings) {
      await this._query(
        `INSERT INTO curated_candidates
           (paper_id, run_id, confidence, predicted_bucket, reasoning,
            predicted_failure_modes, gate_predictions,
            implementability_score, data_requirements_hint)
         VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb)
         ON CONFLICT (paper_id, run_id) DO UPDATE
           SET confidence = EXCLUDED.confidence,
               predicted_bucket = EXCLUDED.predicted_bucket,
               reasoning = EXCLUDED.reasoning,
               predicted_failure_modes = EXCLUDED.predicted_failure_modes,
               gate_predictions = EXCLUDED.gate_predictions,
               implementability_score = EXCLUDED.implementability_score,
               data_requirements_hint = EXCLUDED.data_requirements_hint`,
        [
          r.paper_id, runId, r.confidence, r.predicted_bucket, r.reasoning,
          r.predicted_failure_modes,
          r.gate_predictions ? JSON.stringify(r.gate_predictions) : null,
          r.implementability_score != null ? r.implementability_score : null,
          r.data_requirements_hint ? JSON.stringify(r.data_requirements_hint) : null,
        ]
      );
      // Funnel instrumentation: 'pass' for high bucket, 'reject' otherwise.
      const outcome = r.predicted_bucket === 'high' ? 'pass' : 'reject';
      await emitGateDecision({
        paperId:      r.paper_id,
        gateName:     'curator',
        outcome,
        reasonCode:   `bucket_${r.predicted_bucket}`,
        reasonDetail: r.reasoning || null,
        metadata:     {
          run_id:     runId,
          confidence: r.confidence,
          failure_modes: r.predicted_failure_modes,
        },
      });
    }
  }

  // ── Subagent subprocess helper ─────────────────────────────────────────────

  _spawnSubagent(type, tag, contextObj) {
    return new Promise((resolve, reject) => {
      const tmpFile = `/tmp/curator-ctx-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
      try { fs.writeFileSync(tmpFile, JSON.stringify(contextObj)); }
      catch (e) { return reject(new Error(`ctx file write: ${e.message}`)); }

      const child = spawn('node', [
        NODE_CLI,
        '--type', type,
        '--ticker', tag,
        '--workspace', WORKSPACE,
        '--context-file', tmpFile,
      ], {
        cwd:   OPENCLAW_DIR,
        env:   { ...process.env, OPENCLAW_DIR },
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      let stdout = '';
      let stderr = '';
      child.stdout.on('data', d => { stdout += d; });
      child.stderr.on('data', d => { stderr += d; process.stderr.write(d); });

      child.on('exit', (code) => {
        fs.unlink(tmpFile, () => {});
        if (code !== 0) return reject(new Error(`${type} exit ${code}: ${stderr.slice(0, 400)}`));
        try { resolve(JSON.parse(stdout)); }
        catch { resolve(stdout); }
      });
      child.on('error', (e) => {
        fs.unlink(tmpFile, () => {});
        reject(e);
      });
    });
  }

  // ── Postgres pool ──────────────────────────────────────────────────────────

  async _query(sql, params = []) {
    if (!this._pool) {
      const { Pool } = require('pg');
      this._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
    }
    return this._pool.query(sql, params);
  }
}

module.exports = MastermindCurator;
