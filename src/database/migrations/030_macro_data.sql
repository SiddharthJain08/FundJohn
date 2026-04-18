-- macro_data: time series storage for VIX, VVIX, and other macro indicators.
-- Feeds macro.parquet via sync_master_parquets.py.
CREATE TABLE IF NOT EXISTS macro_data (
    date    DATE        NOT NULL,
    series  TEXT        NOT NULL,
    value   DOUBLE PRECISION NOT NULL,
    source  TEXT        NOT NULL DEFAULT 'yfinance',
    PRIMARY KEY (date, series)
);

CREATE INDEX IF NOT EXISTS macro_data_series_date ON macro_data (series, date);
