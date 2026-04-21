'use strict';

/**
 * Nightly snapshot of curator calibration priors.
 *
 * Reads the live calibration views (bucket / strategy-type / gate) and
 * appends one row per (dimension, key) to curator_priors_snapshot.
 *
 * The curator's prompt already reads the live views. This snapshot gives
 * us a time series so we can see calibration drift across weeks —
 * surfaced back into the prompt via curator_priors_trend.
 *
 * Idempotent per (snapshot_date, dimension, key) — the UNIQUE constraint
 * means re-running the same day is safe.
 */

const { query: dbQuery } = require('../../database/postgres');

async function snapshotBuckets() {
  const { rows } = await dbQuery(`
    SELECT predicted_bucket AS key, n_rated, n_with_truth, n_promoted,
           n_backtest_pass, n_hunter_rejected, promotion_rate
      FROM curator_bucket_calibration
  `);
  let n = 0;
  for (const r of rows) {
    await dbQuery(`
      INSERT INTO curator_priors_snapshot
        (dimension, key, n_rated, n_with_truth, n_promoted,
         n_backtest_pass, n_hunter_rejected, promotion_rate)
      VALUES ('bucket', $1, $2, $3, $4, $5, $6, $7)
      ON CONFLICT (snapshot_date, dimension, key) DO UPDATE SET
        n_rated = EXCLUDED.n_rated,
        n_with_truth = EXCLUDED.n_with_truth,
        n_promoted = EXCLUDED.n_promoted,
        n_backtest_pass = EXCLUDED.n_backtest_pass,
        n_hunter_rejected = EXCLUDED.n_hunter_rejected,
        promotion_rate = EXCLUDED.promotion_rate
    `, [r.key, r.n_rated, r.n_with_truth, r.n_promoted,
        r.n_backtest_pass, r.n_hunter_rejected, r.promotion_rate]);
    n++;
  }
  return n;
}

async function snapshotStrategyTypes() {
  try {
    const { rows } = await dbQuery(`
      SELECT strategy_type AS key, n_rated, n_with_truth, n_promoted,
             n_backtest_pass, n_hunter_rejected, promotion_rate, avg_confidence
        FROM strategy_type_calibration
    `);
    let n = 0;
    for (const r of rows) {
      await dbQuery(`
        INSERT INTO curator_priors_snapshot
          (dimension, key, n_rated, n_with_truth, n_promoted,
           n_backtest_pass, n_hunter_rejected, promotion_rate, avg_confidence)
        VALUES ('strategy_type', $1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (snapshot_date, dimension, key) DO UPDATE SET
          n_rated = EXCLUDED.n_rated,
          n_with_truth = EXCLUDED.n_with_truth,
          n_promoted = EXCLUDED.n_promoted,
          n_backtest_pass = EXCLUDED.n_backtest_pass,
          n_hunter_rejected = EXCLUDED.n_hunter_rejected,
          promotion_rate = EXCLUDED.promotion_rate,
          avg_confidence = EXCLUDED.avg_confidence
      `, [r.key, r.n_rated, r.n_with_truth, r.n_promoted,
          r.n_backtest_pass, r.n_hunter_rejected, r.promotion_rate, r.avg_confidence]);
      n++;
    }
    return n;
  } catch (err) {
    console.warn('[snapshot-priors] strategy_type skipped:', err.message);
    return 0;
  }
}

async function snapshotGates() {
  try {
    const { rows } = await dbQuery(`
      SELECT gate_name AS key, n_observed AS n_rated,
             avg_predicted, actual_pass_rate, over_confidence_bias
        FROM curator_gate_calibration
    `);
    let n = 0;
    for (const r of rows) {
      await dbQuery(`
        INSERT INTO curator_priors_snapshot
          (dimension, key, n_rated, avg_predicted,
           actual_pass_rate, over_confidence_bias)
        VALUES ('gate', $1, $2, $3, $4, $5)
        ON CONFLICT (snapshot_date, dimension, key) DO UPDATE SET
          n_rated = EXCLUDED.n_rated,
          avg_predicted = EXCLUDED.avg_predicted,
          actual_pass_rate = EXCLUDED.actual_pass_rate,
          over_confidence_bias = EXCLUDED.over_confidence_bias
      `, [r.key, r.n_rated, r.avg_predicted, r.actual_pass_rate, r.over_confidence_bias]);
      n++;
    }
    return n;
  } catch (err) {
    console.warn('[snapshot-priors] gate skipped:', err.message);
    return 0;
  }
}

async function snapshotAll() {
  const b = await snapshotBuckets();
  const s = await snapshotStrategyTypes();
  const g = await snapshotGates();
  const total = b + s + g;
  console.log(`[snapshot-priors] ${total} rows written (bucket=${b}, strategy=${s}, gate=${g})`);
  return { bucket: b, strategy_type: s, gate: g, total };
}

if (require.main === module) {
  snapshotAll()
    .then(() => process.exit(0))
    .catch(err => { console.error(err); process.exit(1); });
}

module.exports = { snapshotAll };
