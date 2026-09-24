-- 158: F-5 deferred item from the C1 account breaker (task 7 brief supplement
-- item 8/11, spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md §3 C1).
-- Additive column only: tracks the number of CONSECUTIVE 5-minute ticks a
-- flatten has been pending (fail>0 or partial>0 on the last attempt), so
-- run_once() can escalate to the operator with ONE #trade-reports post after
-- OPENCLAW_ACCOUNT_BREAKER_FLATTEN_ESCALATE_AFTER ticks instead of silently
-- retrying forever with only the [account_breaker] log line as a signal.
ALTER TABLE account_breaker_state
  ADD COLUMN IF NOT EXISTS flatten_attempts INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN account_breaker_state.flatten_attempts IS
  'consecutive pending-flatten ticks (task 7, F-5); reset to 0 on a full flat or an operator re-arm (clear_halt)';
