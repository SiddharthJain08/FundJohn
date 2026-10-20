'use strict';
/**
 * job_stat.js — JS twin of job_stat.py (table scheduled_job_runs, mig 167).
 *
 * Every public function NEVER throws into the caller; failures log a
 * '[job_stat] ...' line. Kill switch: OPENCLAW_JOB_STAT=0. Every query carries
 * query_timeout 5000 so a hung DB cannot pin connections in a shared pool.
 *
 * Semantics for cron rows: runTracked times the CALLBACK. For callbacks that
 * spawn(..., {detached: true}).unref() a finished row means "dispatched", not
 * "child exited". For runPython/execSync and async callbacks it is the real
 * completion. The overdue check therefore detects "cron never fired".
 *
 * started_at/finished_at are taken in JS and passed as parameters (never
 * DEFAULT NOW()): a blocking callback (execSync) delays node-postgres' socket
 * write until it returns, so a DB-side clock would stamp the finish time.
 */
const os = require('os');

const QUERY_TIMEOUT_MS = 5000;

function enabled() {
    return process.env.OPENCLAW_JOB_STAT !== '0';
}

function _q(text, values) {
    return { text, values, query_timeout: QUERY_TIMEOUT_MS };
}

async function recordStart(pool, job, opts = {}) {
    try {
        if (!enabled()) return null;
        const {
            source = 'cron', pid = process.pid, host = os.hostname(),
            note = null, startedAt = new Date(),
        } = opts || {};
        const r = await pool.query(_q(
            'INSERT INTO scheduled_job_runs (job_name, source, started_at, host, pid, note) ' +
            'VALUES ($1,$2,$3,$4,$5,$6) RETURNING id',
            [job, source, startedAt, host, pid, note]));
        const row = r && r.rows && r.rows[0];
        return row ? row.id : null;
    } catch (e) {
        try { console.log(`[job_stat] ${job} start write failed: ${e && e.name}: ${e && e.message}`); } catch (_) { /* ignore */ }
        return null;
    }
}

async function recordFinish(pool, args = {}) {
    const { id = null, job, rc = null, success = null, note = null,
            source = 'cron', finishedAt = new Date() } = args || {};
    try {
        if (!enabled()) return false;
        const ok = success === null || success === undefined ? rc === 0 : success;
        if (id !== null && id !== undefined) {
            await pool.query(_q(
                'UPDATE scheduled_job_runs SET finished_at=$1, success=$2, rc=$3, ' +
                'note=COALESCE($4, note) WHERE id=$5',
                [finishedAt, ok, rc, note, id]));
            return true;
        }
        await pool.query(_q(
            'INSERT INTO scheduled_job_runs (job_name, source, started_at, finished_at, ' +
            'success, rc, host, pid, note) VALUES ($1,$2,$3,$3,$4,$5,$6,$7,$8)',
            [job, source, finishedAt, ok, rc, os.hostname(), process.pid,
             'no-start-row; ' + (note || '')]));
        return true;
    } catch (e) {
        try { console.log(`[job_stat] ${job} finish write failed: ${e && e.name}: ${e && e.message}`); } catch (_) { /* ignore */ }
        return false;
    }
}

// Note building must never throw: a pathological thrown value (Object.create(null))
// would make String() raise a TypeError that masks the original error.
function _note(prefix, err) {
    try {
        return prefix + String((err && err.message) || err).slice(0, 200);
    } catch (_) {
        return prefix + '<unprintable>';
    }
}

function runTracked(pool, job, fn) {
    let startP;
    try {
        startP = recordStart(pool, job, { startedAt: new Date() });
    } catch (_) { startP = Promise.resolve(null); }
    const finish = (rc, success, note) => {
        const finishedAt = new Date();
        try {
            startP.then((id) => recordFinish(pool, { id, job, rc, success, note, finishedAt }))
                  .catch(() => {});
        } catch (_) { /* ignore */ }
    };
    let out;
    try {
        out = fn();
    } catch (err) {
        finish(1, false, _note('threw: ', err));
        throw err;
    }
    if (out && typeof out.then === 'function') {
        out.then(
            () => finish(0, true, 'callback-resolved'),
            (err) => finish(1, false, _note('rejected: ', err)),
        );
        return out;
    }
    finish(0, true, 'callback-returned');
    return out;
}

module.exports = { enabled, recordStart, recordFinish, runTracked, QUERY_TIMEOUT_MS };
