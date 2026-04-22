// Candidate approval worker.
//
// start(job):
//   1. Load a strategy_spec for the candidate (research_candidates row
//      preferred; manifest-only fallback builds a minimal spec).
//   2. Insert into implementation_queue so the existing
//      research-orchestrator pipeline can pick it up.
//   3. Instantiate ResearchOrchestrator and call _codeFromQueue(item,
//      {onPhase}) — it runs StrategyCoder → validate → backtest →
//      promote candidate → paper (via lifecycle.py) → persist metrics.
//   4. On outcome.promoted === true: job=succeeded.
//      Otherwise: job=failed with the reasonCode from the convergence
//      gate.

const fs   = require('fs');
const path = require('path');
const ResearchOrchestrator = require('../research/research-orchestrator');

const MANIFEST_PATH = path.resolve(__dirname, '..', '..', '..', 'src', 'strategies', 'manifest.json');
function readManifestState(sid) {
  try {
    const m = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
    return m.strategies[sid] && m.strategies[sid].state;
  } catch { return null; }
}

async function loadStrategySpec(strategyId, dbQuery) {
  // Preference (a): research_candidates row whose hunter_result_json already
  // has this strategy_id.
  const r = await dbQuery(
    `SELECT candidate_id, hunter_result_json FROM research_candidates
      WHERE hunter_result_json->>'strategy_id' = $1
      ORDER BY submitted_at DESC LIMIT 1`, [strategyId]);
  if (r.rows[0] && r.rows[0].hunter_result_json) {
    return { candidate_id: r.rows[0].candidate_id, ...r.rows[0].hunter_result_json };
  }

  // Preference (b): strategy_hypotheses row.
  try {
    const h = await dbQuery(
      `SELECT * FROM strategy_hypotheses WHERE strategy_id = $1 ORDER BY created_at DESC LIMIT 1`,
      [strategyId]);
    if (h.rows[0]) {
      const row = h.rows[0];
      return {
        strategy_id: strategyId,
        hypothesis_one_liner: row.hypothesis || row.description || 'See strategy_hypotheses row',
        signal_formula_pseudocode: row.formula || row.pseudocode || '',
        regime_applicability: row.regimes || ['LOW_VOL','TRANSITIONING','HIGH_VOL'],
        data_requirements: row.data_requirements || { required: [], optional: [] },
      };
    }
  } catch (_) { /* strategy_hypotheses may not exist yet */ }

  // Preference (c): bail — no viable spec.
  return null;
}

async function start(job, ctx) {
  const { dbQuery, updateJob, finishJob, failJob, emit } = ctx;

  const spec = await loadStrategySpec(job.strategy_id, dbQuery);
  if (!spec) {
    await failJob(job, {
      error: 'missing_strategy_spec',
      hint: `No research_candidates or strategy_hypotheses row found for ${job.strategy_id}. Seed one before approving.`,
    });
    return;
  }
  // Ensure strategy_id is set (strategy_hypotheses fallback may omit it).
  spec.strategy_id = spec.strategy_id || job.strategy_id;

  // Insert/reuse implementation_queue row.
  const { rows: existing } = await dbQuery(
    `SELECT item_id, candidate_id, strategy_spec FROM implementation_queue
      WHERE candidate_id = $1 AND status IN ('pending','coding')
      ORDER BY queued_at DESC LIMIT 1`,
    [spec.candidate_id || null]);
  let item;
  if (existing[0]) {
    item = existing[0];
  } else {
    const { rows } = await dbQuery(
      `INSERT INTO implementation_queue (candidate_id, strategy_spec, status)
       VALUES ($1, $2::jsonb, 'pending')
       RETURNING item_id, candidate_id, strategy_spec`,
      [spec.candidate_id || null, JSON.stringify(spec)]);
    item = rows[0];
  }

  await updateJob(job.job_id, {
    phase: 'strategycoder', progress: 10,
    payload: { strategy_spec: spec, implementation_queue_item: item.item_id },
  });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'running', phase: 'strategycoder', progress: 10 });

  const orch = new ResearchOrchestrator();

  const onPhase = (phase, pct) => {
    updateJob(job.job_id, { phase, progress: pct }).catch(() => {});
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'running', phase, progress: pct });
  };

  try {
    const onChild = (child) => {
      try { require('./index')._internals.registerChild(job.job_id, child); }
      catch (_) {}
    };
    const outcome = await orch._codeFromQueue(item, undefined, undefined, { onPhase, onChild });
    if (outcome && outcome.promoted) {
      // The orchestrator writes manifest state via lifecycle.py execSync and
      // swallows errors non-fatally. If the manifest didn't actually flip
      // (e.g. unknown state value in lifecycle enum), force the transition
      // here via the approvals systemTransition so user-visible state
      // matches the DB.
      const current = readManifestState(job.strategy_id);
      if (current !== 'paper') {
        try {
          const { _internals } = require('./index');
          await _internals.systemTransition(
            job.strategy_id, 'paper', 'system:approve-candidate',
            `Auto-promote after successful backtest (manifest was stuck at ${current})`);
        } catch (e) {
          console.warn('[candidate_approver] forced systemTransition failed:', e.message);
        }
      }
      await finishJob(job.job_id, 'succeeded', { backtest: outcome.backtest_result });
      emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'succeeded', phase: 'done', progress: 100, result: { backtest: outcome.backtest_result } });
    } else {
      await failJob(job, {
        reasonCode: outcome && outcome.reasonCode || 'unknown',
        backtest:   outcome && outcome.backtest_result || null,
        error:      outcome && outcome.error || null,
      });
    }
  } catch (e) {
    await failJob(job, { error: e.message });
  }
}

module.exports = { start, _internals: { loadStrategySpec } };
