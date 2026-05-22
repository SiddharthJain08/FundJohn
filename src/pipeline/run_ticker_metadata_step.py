"""Daily live-mode driver. Reads alpaca_tradable_universe + FMP profile cache
+ prices.parquet → writes today's ticker_metadata_snapshots row."""
from __future__ import annotations
import os, sys, json
from datetime import date
import psycopg2
import pandas as pd
from src.pipeline.ticker_metadata_writer import build_metadata_rows, write_snapshots

DSN = os.environ["POSTGRES_URI"]
PRICES_PARQUET = "/root/openclaw/data/master/prices.parquet"
FMP_PROFILE_CACHE = "/root/openclaw/data/.cache/fmp_profile.json"
OPTIONS_ELIGIBILITY_CACHE = "/root/openclaw/data/.cache/options_eligibility.json"

def fetch_alpaca_universe():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT symbol, asset_class, exchange, status, tradable, shortable,
                   fractionable, easy_to_borrow, first_seen_at, last_seen_at
            FROM alpaca_tradable_universe
            WHERE status='active'
        """)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def adv_usd_from_parquet(symbols):
    if not os.path.exists(PRICES_PARQUET):
        return {s: {"adv_usd_20d": None} for s in symbols}
    # prices.parquet uses "ticker" column (not "symbol")
    df = pd.read_parquet(PRICES_PARQUET, columns=["ticker", "date", "close", "volume"])
    df = df[df["ticker"].isin(symbols)]
    df["dollar_volume"] = df["close"] * df["volume"]
    last_20 = (df.sort_values("date").groupby("ticker")
                 .tail(20).groupby("ticker")["dollar_volume"].mean())
    return {s: {"adv_usd_20d": float(last_20.get(s, 0.0))} for s in symbols}

def main():
    today = date.today()
    alpaca_rows = fetch_alpaca_universe()
    symbols = [r["symbol"] for r in alpaca_rows]
    fmp_profile = load_json(FMP_PROFILE_CACHE)
    options_cache = load_json(OPTIONS_ELIGIBILITY_CACHE)
    prices_parquet = adv_usd_from_parquet(symbols)
    rows = build_metadata_rows(
        today, alpaca_rows, fmp_profile, prices_parquet, options_cache,
        source_tag="live_daily",
    )
    n = write_snapshots(DSN, rows)
    print(json.dumps({"date": str(today), "rows": n}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
