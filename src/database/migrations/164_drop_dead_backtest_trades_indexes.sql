-- 164: drop two strategy_backtest_trades indexes with idx_scan = 0 (lifetime, measured 2026-10-19:
-- idx_backtest_trades_strategy 321 MB, idx_backtest_trades_regime 226 MB). Spec 2026-10-19 §1 A5.
-- Live DB: the controller runs DROP INDEX CONCURRENTLY off-hours (R1) BEFORE the next johnbot restart;
-- this file is the idempotent record so a fresh clone matches. migrate() runs every file on each
-- johnbot start inside one implicit transaction, hence no CONCURRENTLY and a bounded lock wait.
BEGIN;
SET LOCAL lock_timeout = '5s';
DROP INDEX IF EXISTS idx_backtest_trades_strategy;
DROP INDEX IF EXISTS idx_backtest_trades_regime;
COMMIT;
