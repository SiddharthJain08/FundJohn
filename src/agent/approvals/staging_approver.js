// Staging approval worker.
//
// start(job) — called once per approve click:
//   1. Read <strategy>.requirements.json
//   2. Compute missing = sources (required ∪ optional) with no recent
//      data_coverage rows
//   3. Any missing source that isn't in data/master/schema_registry.json
//      is unsupported → fail the job (admin must add a collector)
//   4. For supported missing sources, INSERT approved rows into
//      data_ingestion_queue so the existing datawiring/collector pipeline
//      picks them up
//   5. Move job to phase='awaiting_snapshot'
//
// tick(job) — called every 60s by the poll loop:
//   If all required sources now have recent coverage, promote
//   staging → candidate and finish the job.

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = path.resolve(__dirname, '..', '..', '..');
const REQ_DIR       = path.join(OPENCLAW_DIR, 'src', 'strategies', 'implementations');
const SCHEMA_PATH   = path.join(OPENCLAW_DIR, 'data', 'master', 'schema_registry.json');

// How fresh does data_coverage have to be to count as "recent"?
// Matches collector's own completion tolerance.
const COVERAGE_LAG_DAYS = 7;

function readRequirements(strategyId) {
  // requirements.json uses the snake_case filename base, not the strategy_id.
  // Look it up via manifest metadata.canonical_file first, else fall back to
  // lowercased strategy_id.
  const manifest = JSON.parse(
    fs.readFileSync(path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json'), 'utf8'));
  const rec = manifest.strategies[strategyId] || {};
  const canonical = (rec.metadata && rec.metadata.canonical_file) || `${strategyId.toLowerCase()}.py`;
  const base = canonical.replace(/\.py$/, '');
  const p = path.join(REQ_DIR, `${base}.requirements.json`);
  if (!fs.existsSync(p)) return { required: [], optional: [] };
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  return {
    required: Array.isArray(j.required) ? j.required : [],
    optional: Array.isArray(j.optional) ? j.optional : [],
  };
}

function readSchemaRegistry() {
  if (!fs.existsSync(SCHEMA_PATH)) return {};
  return JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));
}

async function sourcesMissingCoverage(sources, dbQuery) {
  // A source is "covered" if any row in data_coverage with data_type=<source>
  // has date_to within COVERAGE_LAG_DAYS of today.
  const cutoff = new Date(Date.now() - COVERAGE_LAG_DAYS * 86_400_000)
    .toISOString().slice(0, 10);
  const missing = [];
  for (const src of sources) {
    const { rows } = await dbQuery(
      `SELECT 1 FROM data_coverage WHERE data_type=$1 AND date_to >= $2 LIMIT 1`,
      [src, cutoff]);
    if (!rows.length) missing.push(src);
  }
  return missing;
}

async function start(job, ctx) {
  const { dbQuery, updateJob, finishJob, failJob, systemTransition, emit } = ctx;

  const reqs      = readRequirements(job.strategy_id);
  const all       = [...new Set([...reqs.required, ...reqs.optional])];
  const missing   = await sourcesMissingCoverage(all, dbQuery);

  if (!missing.length) {
    // Already collectable — flip state immediately.
    await systemTransition(job.strategy_id, 'candidate', 'system:approve-staging',
      'No missing data sources — approval auto-promoted to candidate');
    await finishJob(job.job_id, 'succeeded', { no_setup_needed: true });
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'succeeded', phase: 'done', progress: 100 });
    return;
  }

  const schema = readSchemaRegistry();
  const unsupported = missing.filter(s => !schema[s]);
  if (unsupported.length) {
    await failJob(job, {
      error: 'unsupported_source',
      unsupported,
      hint: `Add a collector module for ${unsupported.join(', ')} in data/master/schema_registry.json before approving.`,
    });
    return;
  }

  // All missing sources are supported — register for collection.
  const insertedIds = [];
  for (const src of missing) {
    // Upsert-style: if a PENDING/APPROVED row already exists for this source,
    // reuse it; otherwise create a new APPROVED row.
    const { rows: existing } = await dbQuery(
      `SELECT request_id FROM data_ingestion_queue
        WHERE column_name=$1 AND status IN ('PENDING','APPROVED') LIMIT 1`, [src]);
    if (existing.length) {
      await dbQuery(
        `UPDATE data_ingestion_queue
            SET status='APPROVED',
                approved_by=COALESCE(approved_by,$2),
                approved_at=COALESCE(approved_at,NOW())
          WHERE request_id=$1`,
        [existing[0].request_id, job.actor]).catch(() => {});
      insertedIds.push(existing[0].request_id);
    } else {
      const { rows } = await dbQuery(
        `INSERT INTO data_ingestion_queue (column_name, status, approved_by, approved_at)
         VALUES ($1, 'APPROVED', $2, NOW()) RETURNING request_id`,
        [src, job.actor]);
      insertedIds.push(rows[0].request_id);
    }
  }

  await updateJob(job.job_id, {
    phase: 'awaiting_snapshot',
    progress: 30,
    payload: { missing_sources: missing, inserted_queue_ids: insertedIds },
  });
  emit({
    type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
    status: 'running', phase: 'awaiting_snapshot', progress: 30,
    payload: { missing_sources: missing },
  });
}

async function tick(job, ctx) {
  if (job.phase !== 'awaiting_snapshot') return; // nothing to do
  const { dbQuery, finishJob, systemTransition, emit } = ctx;

  const reqs = readRequirements(job.strategy_id);
  const needed = [...new Set([...reqs.required, ...reqs.optional])];
  const stillMissing = await sourcesMissingCoverage(needed, dbQuery);

  // Progress fraction for the chip.
  const total  = needed.length || 1;
  const ready  = total - stillMissing.length;
  const pct    = 30 + Math.round((ready / total) * 60);
  if (stillMissing.length) {
    // Update progress even if not done.
    await dbQuery(
      `UPDATE strategy_approval_jobs SET progress=$2 WHERE job_id=$1 AND progress<$2`,
      [job.job_id, pct]).catch(() => {});
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'running', phase: 'awaiting_snapshot', progress: pct, payload: { missing_sources: stillMissing } });
    return;
  }

  // All covered — promote.
  await systemTransition(job.strategy_id, 'candidate', 'system:approve-staging',
    'Data collection complete — approval auto-promoted to candidate');
  await finishJob(job.job_id, 'succeeded', { promoted_at: new Date().toISOString() });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'succeeded', phase: 'done', progress: 100 });
}

module.exports = { start, tick, _internals: { readRequirements, readSchemaRegistry, sourcesMissingCoverage } };
