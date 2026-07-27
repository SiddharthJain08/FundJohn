-- 145: broker-fill truth on the parity ledger (fix 7, 2026-07-27).
-- parity_mark deliberately books the OFFICIAL close as mark_entry_price /
-- fill_price so signal_pnl reproduces backtest bracket geometry (the parity
-- guarantee). That left live execution cost invisible: no column carried what
-- we ACTUALLY paid. broker_fill_price = alpaca_submissions.filled_avg_price
-- for the same (target_date, ticker); fill_slippage_bps = signed adverse gap
-- vs the official close (positive = paid worse than the mark). Populated by
-- parity_mark.backfill_broker_fill_truth on each EOD pass (self-healing over
-- a trailing window — reconcile may land fills after the first pass).
ALTER TABLE execution_signals ADD COLUMN IF NOT EXISTS broker_fill_price NUMERIC;
ALTER TABLE execution_signals ADD COLUMN IF NOT EXISTS fill_slippage_bps NUMERIC;
