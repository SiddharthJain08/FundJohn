"""Daily live-mode driver. Reads alpaca_tradable_universe + FMP profile cache
+ prices.parquet → writes today's ticker_metadata_snapshots row."""
from __future__ import annotations
import os, sys, json
from datetime import date, timedelta
import logging
import psycopg2
import pandas as pd
import pyarrow.parquet as pq
from src.data.parquet_store import ticker_window_tail_rows
from src.pipeline.ticker_metadata_writer import build_metadata_rows, write_snapshots

log = logging.getLogger(__name__)
ADV_WINDOW_CALENDAR_DAYS = 60
ADV_LAST_N = 20
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

def adv_usd_from_parquet(symbols, as_of: date | None = None):
    """20-day ADV. Pass 1: 60-day windowed aggregate. Pass 2: tickers with <20
    window rows (stale/sparse) get the legacy tail(20) math over their full
    history, so output is identical to the legacy full read."""
    if not os.path.exists(PRICES_PARQUET):
        print("[adv_window] adv_window_hits=0 adv_stale_fallback=0 missing_file=1",
              file=sys.stderr, flush=True)
        return {s: {"adv_usd_20d": None} for s in symbols}
    as_of = as_of or date.today()
    cutoff = (as_of - timedelta(days=ADV_WINDOW_CALENDAR_DAYS)).isoformat()
    symset = set(symbols)
    rows = ticker_window_tail_rows(PRICES_PARQUET, cutoff=cutoff, upto=as_of.isoformat(),
                                   last_n=ADV_LAST_N)
    rows = rows[rows["ticker"].isin(symset)]
    # Only tickers whose window holds >= 20 rows have the same last-20 as the full history.
    full = rows[rows["n_rows"] >= ADV_LAST_N]
    hit_set = set(full["ticker"])
    fallback = sorted(symset - hit_set)
    parts = [full[["ticker", "date", "close", "volume"]]]
    if fallback:
        # prices.parquet uses "ticker" column (not "symbol")
        parts.append(pq.read_table(PRICES_PARQUET, columns=["ticker", "date", "close", "volume"],
                                   filters=[("ticker", "in", fallback)]).to_pandas())
    df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0].copy()
    # Verbatim legacy math: identical summation order => bit-identical result.
    df["dollar_volume"] = df["close"] * df["volume"]
    last_20 = (df.sort_values("date").groupby("ticker")
                 .tail(ADV_LAST_N).groupby("ticker")["dollar_volume"].mean())
    print(f"[adv_window] adv_window_hits={len(hit_set)} adv_stale_fallback={len(fallback)}",
          file=sys.stderr, flush=True)
    return {s: {"adv_usd_20d": float(last_20.get(s, 0.0))} for s in symbols}

def main():
    today = date.today()
    alpaca_rows = fetch_alpaca_universe()
    symbols = [r["symbol"] for r in alpaca_rows]
    fmp_profile = load_json(FMP_PROFILE_CACHE)
    options_cache = load_json(OPTIONS_ELIGIBILITY_CACHE)
    prices_parquet = adv_usd_from_parquet(symbols, as_of=today)
    from src.pipeline.market_cap_lookup import build_market_cap_lookup
    market_caps = build_market_cap_lookup(symbols, today)
    rows = build_metadata_rows(
        today, alpaca_rows, fmp_profile, prices_parquet, options_cache,
        source_tag="live_daily",
        market_cap_lookup=market_caps,
    )
    n = write_snapshots(DSN, rows)
    print(json.dumps({"date": str(today), "rows": n}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
