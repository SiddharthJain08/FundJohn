'use strict';

const { Pool } = require('pg');
const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

const ResearchSession = {
    async create(workspaceId) {
        const r = await pool.query(
            `INSERT INTO research_sessions (workspace_id) VALUES ($1) RETURNING *`,
            [workspaceId]
        );
        return r.rows[0];
    },

    async updateState(id, state, phase, tokensUsed) {
        await pool.query(
            `UPDATE research_sessions
             SET state=$1, phase=$2, total_tokens_used=total_tokens_used+$3
             WHERE id=$4`,
            [JSON.stringify(state), phase, tokensUsed || 0, id]
        );
    },

    async saveHypothesis(sessionId, workspaceId, h) {
        const r = await pool.query(
            `INSERT INTO strategy_hypotheses
             (session_id, workspace_id, name, description, tier, data_requirements, implementation_complexity, hypothesis_score)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id`,
            [sessionId, workspaceId, h.name, h.description, h.tier,
             JSON.stringify(h.data_requirements), h.complexity, h.score]
        );
        return r.rows[0].id;
    },

    async saveBacktest(hypothesisId, workspaceId, result) {
        await pool.query(
            `INSERT INTO backtest_results
             (hypothesis_id, workspace_id, backtest_period_start, backtest_period_end,
              universe, total_trades, win_rate, avg_win_pct, avg_loss_pct, sharpe_ratio,
              max_drawdown_pct, annualized_return_pct, benchmark_return_pct, information_ratio,
              calmar_ratio, avg_holding_days, profit_factor, walk_forward_score,
              statistical_significance, passed_validation, rejection_reason, full_results)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)`,
            [hypothesisId, workspaceId, result.start_date, result.end_date,
             result.universe.join(','), result.total_trades, result.win_rate,
             result.avg_win_pct, result.avg_loss_pct, result.sharpe_ratio,
             result.max_drawdown_pct, result.annualized_return_pct, result.benchmark_return_pct,
             result.information_ratio, result.calmar_ratio, result.avg_holding_days,
             result.profit_factor, result.walk_forward_score || 0, result.p_value,
             result.passed_validation, result.rejection_reason,
             JSON.stringify(result.trade_log || [])]
        );
    },

    async saveEmergencyAlert(workspaceId, alert) {
        const r = await pool.query(
            `INSERT INTO emergency_alerts
             (workspace_id, ticker, alert_type, severity, description, evidence, report_path)
             VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`,
            [workspaceId, alert.ticker, alert.type, alert.severity,
             alert.description, JSON.stringify(alert.evidence), alert.report_path || null]
        );
        return r.rows[0].id;
    },
};

module.exports = ResearchSession;
