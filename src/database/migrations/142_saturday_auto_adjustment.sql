-- 142: Saturday full-auto adjustments (2026-07-14 operator directive).
--
-- strategy_sizing_recommendations:
--   * confidence        — memo recommendations.confidence persisted per rec;
--                         the Monday-handoff SIZE flow only consumes rows
--                         inserted as 'pending' (confidence strictly > 0.8).
--   * coupling_outcome  — bracket decision from the Saturday backtest-coupling
--                         step ('applied' / 'rejected'); decoupled from
--                         action_taken so a bracket reject no longer silently
--                         drops the size rec, and low-confidence 'noted' rows
--                         still get their brackets backtested.
--   * action_taken CHECK gains 'noted' (low-confidence — excluded from the
--                         handoff, surfaced on the dashboard, re-evaluated by
--                         the next weekly review).

ALTER TABLE strategy_sizing_recommendations
    ADD COLUMN IF NOT EXISTS confidence NUMERIC,
    ADD COLUMN IF NOT EXISTS coupling_outcome TEXT;

ALTER TABLE strategy_sizing_recommendations
    DROP CONSTRAINT IF EXISTS strategy_sizing_recommendations_action_taken_check;
ALTER TABLE strategy_sizing_recommendations
    ADD CONSTRAINT strategy_sizing_recommendations_action_taken_check
    CHECK (action_taken IN ('pending','applied','ignored','superseded','noted'));

COMMENT ON COLUMN strategy_sizing_recommendations.confidence IS
    'Memo recommendations.confidence (0..1); strictly > 0.8 inserts as pending (size flows to Monday handoff), else noted.';
COMMENT ON COLUMN strategy_sizing_recommendations.coupling_outcome IS
    'Saturday backtest-coupling bracket decision: applied | rejected | NULL (not yet coupled). Independent of action_taken.';
