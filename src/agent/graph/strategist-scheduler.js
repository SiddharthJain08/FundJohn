/**
 * Strategist Scheduler — v2
 *
 * Activation conditions (ALL must be true):
 *   1. Off-hours (6pm–6am ET weekdays, all weekend) OR force=true
 *   2. >= 20% of daily token budget remaining
 *   3. Pipeline is idle (no other subagents running)
 *
 * During a session, the strategist also:
 *   4. Yields immediately if any other agent starts
 *   5. Pauses if token budget drops below 20%
 *   6. Resumes automatically when conditions are met again
 */

'use strict';

const { getClient: redisClient } = require('../../database/redis');
const { Pool }         = require('pg');
const pipelineActivity = require('../middleware/pipeline-activity');
const tokenBudget      = require('../middleware/token-budget');

const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

function todayStr() {
    return new Date().toISOString().slice(0, 10);
}

function isOffHours() {
    const now  = new Date();
    const et   = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const hour = et.getHours();
    const day  = et.getDay(); // 0=Sun, 6=Sat
    if (day === 0 || day === 6) return true;
    return hour >= 18 || hour < 6;
}

function minutesUntilOffHours() {
    if (isOffHours()) return 0;
    const now  = new Date();
    const et   = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    return (18 - et.getHours()) * 60 - et.getMinutes();
}

// Full pre-flight check — all conditions must pass
async function canActivate(workspaceId, force = false) {
    const reasons = [];

    // 1. Off-hours check
    if (!force && !isOffHours()) {
        const mins = minutesUntilOffHours();
        reasons.push(`Not off-hours. Next window in ${mins} min (6pm ET).`);
    }

    // 2. Token budget check
    const budget = await tokenBudget.getBudgetStatus(workspaceId);
    if (!budget.budget_ok) {
        reasons.push(`Token budget insufficient: ${budget.formatted} (need >= 20%)`);
    }

    // 3. Pipeline idle check
    const pipeline = await pipelineActivity.isPipelineIdle(workspaceId);
    if (!pipeline.idle) {
        reasons.push(`Pipeline busy: ${pipeline.formatted}`);
    }

    const allowed = reasons.length === 0;
    return {
        allowed,
        reasons,
        budget,
        pipeline,
        off_hours: isOffHours(),
        summary: allowed
            ? `✅ All conditions met — strategist may activate`
            : `⛔ Cannot activate:\n${reasons.map(r => `  • ${r}`).join('\n')}`,
    };
}

// Mid-session health check — call this between every major step
async function shouldContinue(workspaceId) {
    const budget   = await tokenBudget.getBudgetStatus(workspaceId);
    const pipeline = await pipelineActivity.isPipelineIdle(workspaceId);

    if (budget.critical) {
        return {
            continue: false,
            pause_reason: `TOKEN_CRITICAL: ${budget.formatted} — pausing to protect pipeline`,
            budget,
            pipeline,
        };
    }

    if (!budget.budget_ok) {
        return {
            continue: false,
            pause_reason: `TOKEN_LOW: ${budget.formatted} — budget below 20% threshold`,
            budget,
            pipeline,
        };
    }

    if (!pipeline.idle) {
        return {
            continue: false,
            pause_reason: `PIPELINE_ACTIVE: ${pipeline.formatted} — yielding to research agents`,
            budget,
            pipeline,
        };
    }

    return {
        continue: true,
        steps_remaining: tokenBudget.estimateStepsRemaining(budget),
        budget,
        pipeline,
    };
}

async function getResumableSession(workspaceId) {
    const result = await pool.query(
        `SELECT * FROM research_sessions
         WHERE workspace_id=$1 AND status='paused'
         ORDER BY paused_at DESC LIMIT 1`,
        [workspaceId]
    );
    return result.rows[0] || null;
}

async function startSession(workspaceId, force = false) {
    const check = await canActivate(workspaceId, force);

    if (!check.allowed) {
        return {
            started: false,
            reason:  check.summary,
            check,
        };
    }

    const existing = await getResumableSession(workspaceId);
    if (existing) {
        await pool.query(
            `UPDATE research_sessions
             SET status='active', resumed_at=NOW(), pause_reason=NULL
             WHERE id=$1`,
            [existing.id]
        );
        await redisClient().set(`strategist:session:${workspaceId}`, existing.id);
        return {
            started:    true,
            mode:       'RESUMED',
            session_id: existing.id,
            state:      existing.state,
            check,
        };
    }

    const newSession = await pool.query(
        `INSERT INTO research_sessions (workspace_id, status, phase, state)
         VALUES ($1,'active','EXPLORE','{}') RETURNING *`,
        [workspaceId]
    );
    const session = newSession.rows[0];
    await redisClient().set(`strategist:session:${workspaceId}`, session.id);
    return {
        started:    true,
        mode:       'NEW',
        session_id: session.id,
        state:      {},
        check,
    };
}

async function pauseSession(workspaceId, currentState, reason = 'manual') {
    const sessionId = await redisClient().get(`strategist:session:${workspaceId}`);
    if (!sessionId) return { paused: false, reason: 'No active session' };

    await pool.query(
        `UPDATE research_sessions
         SET status='paused', paused_at=NOW(), state=$1, pause_reason=$2
         WHERE id=$3`,
        [JSON.stringify(currentState), reason, sessionId]
    );
    await redisClient().del(`strategist:session:${workspaceId}`);
    return { paused: true, session_id: sessionId, reason };
}

async function completeSession(workspaceId, notes) {
    const sessionId = await redisClient().get(`strategist:session:${workspaceId}`);
    if (!sessionId) return;
    await pool.query(
        `UPDATE research_sessions
         SET status='completed', completed_at=NOW(), session_notes=$1
         WHERE id=$2`,
        [notes, sessionId]
    );
    await redisClient().del(`strategist:session:${workspaceId}`);
}

async function triggerEmergencyAlert(workspaceId, alert) {
    await redisClient().lpush(
        `strategist:emergency:${workspaceId}`,
        JSON.stringify({ ...alert, triggered_at: new Date().toISOString() })
    );
    await redisClient().set(`strategist:emergency_pending:${workspaceId}`, '1', 'EX', 86400);
}

async function checkEmergencyAlerts(workspaceId) {
    const pending = await redisClient().get(`strategist:emergency_pending:${workspaceId}`);
    if (!pending) return [];
    const alerts = [];
    let alert;
    while ((alert = await redisClient().lpop(`strategist:emergency:${workspaceId}`))) {
        alerts.push(JSON.parse(alert));
    }
    if (alerts.length === 0) await redisClient().del(`strategist:emergency_pending:${workspaceId}`);
    return alerts;
}

module.exports = {
    isOffHours,
    minutesUntilOffHours,
    canActivate,
    shouldContinue,
    startSession,
    pauseSession,
    completeSession,
    triggerEmergencyAlert,
    checkEmergencyAlerts,
};
