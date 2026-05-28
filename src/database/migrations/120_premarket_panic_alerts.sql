-- src/database/migrations/120_premarket_panic_alerts.sql
-- Pre-market sentiment panic scanner: audit log of every scan output.
-- One row per (scan_ts, ticker). Realized PnL columns are filled by EOD job.

CREATE TABLE IF NOT EXISTS premarket_panic_alerts (
    id                          BIGSERIAL PRIMARY KEY,
    scan_ts                     TIMESTAMPTZ NOT NULL,
    scan_label                  TEXT NOT NULL,
    trading_day                 DATE NOT NULL,
    ticker                      TEXT NOT NULL,
    held_qty                    NUMERIC NOT NULL,
    avg_entry_price             NUMERIC,
    news_count_window           INT NOT NULL DEFAULT 0,
    news_finbert_neg_ratio      NUMERIC,
    news_finbert_mean_score     NUMERIC,
    social_post_count_window    INT NOT NULL DEFAULT 0,
    social_bear_ratio           NUMERIC,
    panic_score                 NUMERIC NOT NULL,
    advisory_fired              BOOLEAN NOT NULL DEFAULT FALSE,
    sonnet_verdict              TEXT,
    sonnet_severity             INT,
    sonnet_rationale            TEXT,
    sonnet_evidence_uuids       UUID[],
    sonnet_cost_usd             NUMERIC,
    autoclose_fired             BOOLEAN NOT NULL DEFAULT FALSE,
    autoclose_liquidation_id    UUID REFERENCES alpaca_liquidations(id),
    realized_open_to_open_pct   NUMERIC,
    realized_open_to_close_pct  NUMERIC,
    realized_backfilled_at      TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS premarket_panic_alerts_day_ticker
    ON premarket_panic_alerts(trading_day, ticker);
CREATE INDEX IF NOT EXISTS premarket_panic_alerts_scan_ts
    ON premarket_panic_alerts(scan_ts);
