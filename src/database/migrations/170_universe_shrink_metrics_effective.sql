-- 170: universe_shrink_metrics_effective (spec 2026-11-24 shrink Sharpe marks fix, Part A).
-- universe_shrink_metrics rows were computed with a smear methodology that
-- differs from the backtest of record. A ladder tier's trades are a SUBSET of
-- the full run's trades, so an equal trade_count means the identical trade
-- set: there the run row (methodology of record) is substituted. Rows with
-- different counts pass through unchanged. from_run_row marks substitution.
CREATE OR REPLACE VIEW universe_shrink_metrics_effective AS
SELECT m.id, m.strategy_id, m.run_id, m.tier, m.regime_state, m.chosen, m.candidate_set_id, m.rec_id, m.computed_at,
       m.trade_count,
       CASE WHEN br.trade_count = m.trade_count THEN br.sharpe            ELSE m.sharpe            END AS sharpe,
       CASE WHEN br.trade_count = m.trade_count THEN br.max_dd_pct        ELSE m.max_dd_pct        END AS max_dd_pct,
       CASE WHEN br.trade_count = m.trade_count THEN br.hit_rate          ELSE m.win_rate          END AS win_rate,
       CASE WHEN br.trade_count = m.trade_count THEN br.sortino           ELSE m.sortino           END AS sortino,
       CASE WHEN br.trade_count = m.trade_count THEN br.calmar            ELSE m.calmar            END AS calmar,
       CASE WHEN br.trade_count = m.trade_count THEN br.avg_holding_days  ELSE m.mean_holding_days END AS mean_holding_days,
       CASE WHEN br.trade_count = m.trade_count THEN br.return_pct        ELSE m.return_pct        END AS return_pct,
       (br.trade_count = m.trade_count) IS TRUE AS from_run_row
  FROM universe_shrink_metrics m
  LEFT JOIN strategy_backtest_regimes br ON br.run_id = m.run_id AND br.regime_state = m.regime_state
 WHERE m.regime_state <> 'TOTAL'
UNION ALL
SELECT m.id, m.strategy_id, m.run_id, m.tier, m.regime_state, m.chosen, m.candidate_set_id, m.rec_id, m.computed_at,
       m.trade_count,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_sharpe       ELSE m.sharpe            END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_max_dd_pct   ELSE m.max_dd_pct        END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_hit_rate     ELSE m.win_rate          END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_sortino      ELSE m.sortino           END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_calmar       ELSE m.calmar            END,
       CASE WHEN r.total_trades = m.trade_count THEN r.avg_holding_days   ELSE m.mean_holding_days END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_return_pct   ELSE m.return_pct        END,
       (r.total_trades = m.trade_count) IS TRUE
  FROM universe_shrink_metrics m
  LEFT JOIN strategy_backtest_runs r ON r.run_id = m.run_id
 WHERE m.regime_state = 'TOTAL';
