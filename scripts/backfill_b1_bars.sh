#!/usr/bin/env bash
# Curated 30-min bars backfill for B1 case studies, via the Alpaca ingester
# (the old Polygon ingest_prices_30m.py is dead — POLYGON_API_KEY purged in SP-1).
# APPEND-ONLY into data/master/prices_30m.parquet (the ingester enforces a NEVER-DELETE
# guard: it refuses any write that would drop an existing ticker or shrink rows).
# nice'd (2-core/8GB box). Do NOT run during 19:55–20:30 UTC (the live 3:55 fill → 4:15
# EOD compute cycle).
set -euo pipefail
cd "$(dirname "$0")/.."
UNIV="${1:-/tmp/b1_universe.txt}"
FROM="${2:-2026-03-01}"
TO="${3:-2026-05-30}"
echo "[b1-backfill] tickers=$(wc -l < "$UNIV") from=$FROM to=$TO (Alpaca, append-only)"
PYTHONPATH=src nice -n 19 python3 -m ingestion.ingest_prices_30m_alpaca --universe-file "$UNIV" --from "$FROM" --to "$TO"
echo "[b1-backfill] done"
