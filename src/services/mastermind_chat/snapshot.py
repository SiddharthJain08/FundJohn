#!/usr/bin/env python3
"""Emit a compact JSON snapshot of dashboard state for MasterMindJohn's
session preamble.

Queries the local OpenClaw Postgres directly via psycopg2 — no SSH-tunnel
fallback, no dashboard-venv dependency. Tolerant of partial failures: any
missing section is replaced with {"error": "<reason>"} rather than
crashing the snapshot.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

PG_URI = os.environ.get(
    "POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw"
)

SCHEMA_REGISTRY = os.environ.get(
    "OPENCLAW_SCHEMA_REGISTRY",
    "/root/openclaw/data/master/schema_registry.json",
)
PARQUET_ROOT = os.environ.get("OPENCLAW_PARQUET_ROOT", "/root/openclaw/data/master")


@contextmanager
def cursor():
    conn = psycopg2.connect(PG_URI, connect_timeout=5)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


def _rows(sql: str, params=None, limit=None) -> list[dict] | dict:
    try:
        with cursor() as cur:
            cur.execute(sql, params or ())
            rows = [dict(r) for r in cur.fetchall()]
            if limit:
                rows = rows[:limit]
            return rows
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _scalar(sql: str, params=None) -> dict:
    try:
        with cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _data_catalog() -> dict:
    """Derive a live data catalog from schema_registry.json + parquet footers.

    One entry per dataset: columns, row_count, min_date, max_date, ticker_count.
    Uses pyarrow.parquet.ParquetFile footer stats (fast; no full scan).
    """
    out: dict = {}
    try:
        with open(SCHEMA_REGISTRY) as fh:
            registry = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"registry: {type(exc).__name__}: {exc}"}

    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        pq = None

    for dataset, meta in registry.items():
        columns = meta.get("columns", []) if isinstance(meta, dict) else []
        entry: dict = {"columns": columns, "column_count": len(columns)}
        path = os.path.join(PARQUET_ROOT, f"{dataset}.parquet")
        if not os.path.exists(path):
            entry["status"] = "absent"
            out[dataset] = entry
            continue
        entry["path"] = path
        try:
            entry["size_mb"] = round(os.path.getsize(path) / 1_048_576, 1)
        except OSError:
            pass
        if pq is None:
            entry["status"] = "pyarrow_missing"
            out[dataset] = entry
            continue
        try:
            pf = pq.ParquetFile(path)
            entry["row_count"] = pf.metadata.num_rows
            date_col = None
            for c in ("date", "pnl_date", "run_date", "submitted_at"):
                if c in columns:
                    date_col = c
                    break
            if date_col:
                mins, maxs = [], []
                for rg in range(pf.num_row_groups):
                    stats = pf.metadata.row_group(rg).column(
                        columns.index(date_col)
                    ).statistics
                    if stats and stats.has_min_max:
                        mins.append(str(stats.min))
                        maxs.append(str(stats.max))
                if mins:
                    entry["min_date"] = min(mins)
                    entry["max_date"] = max(maxs)
            if "ticker" in columns:
                try:
                    tcol = pf.read(columns=["ticker"]).column("ticker")
                    entry["ticker_count"] = len(set(tcol.to_pylist()))
                except Exception:  # noqa: BLE001
                    pass
            entry["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            entry["status"] = f"error: {type(exc).__name__}: {exc}"
        out[dataset] = entry
    return out


def build() -> dict:
    return {
        "data_catalog": _data_catalog(),
        "recent_campaigns": _rows(
            """
            SELECT id::text, name, request_text, status,
                   progress_json, created_at, completed_at
              FROM research_campaigns
             ORDER BY created_at DESC
             LIMIT 10
            """
        ),
        "strategies": _rows(
            """
            SELECT id, name, status, tier, live_days, live_sharpe,
                   backtest_sharpe, backtest_return_pct, backtest_max_dd_pct,
                   signal_frequency, universe
              FROM strategy_registry
             WHERE status IN ('live','monitoring','candidate','pending_approval')
             ORDER BY status, id
             LIMIT 80
            """
        ),
        "portfolio": {
            "open_signals": _scalar(
                """
                SELECT COUNT(*)::int AS open_count,
                       COUNT(*) FILTER (WHERE direction='LONG')::int AS longs,
                       COUNT(*) FILTER (WHERE direction='SHORT')::int AS shorts
                  FROM execution_signals
                 WHERE status='open'
                """
            ),
            "recent_pnl": _rows(
                """
                SELECT signal_id, strategy_id, pnl_date, pnl_value
                  FROM signal_pnl
                 ORDER BY pnl_date DESC, signal_id DESC
                 LIMIT 30
                """
            ),
        },
        "regime": _rows(
            """
            SELECT * FROM market_regime
             ORDER BY regime_date DESC
             LIMIT 1
            """
        ),
        "research_queue": _rows(
            """
            SELECT rc.candidate_id::text, rc.source_url, rc.status, rc.kind,
                   rc.submitted_at, rc.priority,
                   rcp.title, rcp.venue, rcp.published_date
              FROM research_candidates rc
              LEFT JOIN research_corpus rcp USING (source_url)
             ORDER BY rc.submitted_at DESC
             LIMIT 30
            """
        ),
        "staging": _rows(
            """
            SELECT id::text, name, thesis, status, created_at
              FROM strategy_staging
             ORDER BY (status='pending') DESC, created_at DESC
             LIMIT 20
            """
        ),
        "recent_memos": _rows(
            """
            SELECT id::text, strategy_id, memo_date, cost_usd,
                   recommendations->'position_size' AS size_rec,
                   substring(markdown_body from 1 for 400) AS memo_preview
              FROM strategy_memos
             ORDER BY memo_date DESC, created_at DESC
             LIMIT 8
            """
        ),
        "recent_sizing_recs": _rows(
            """
            SELECT strategy_id, rec_date, current_size_pct, recommended_size_pct,
                   size_delta_pct, stop_delta_pct, target_delta_pct,
                   hold_days_delta, action_taken, reasoning
              FROM strategy_sizing_recommendations
             WHERE rec_date >= CURRENT_DATE - 14
             ORDER BY rec_date DESC, strategy_id
             LIMIT 20
            """
        ),
        "recent_paper_expansions": _rows(
            """
            SELECT id::text, run_date, status, papers_imported, papers_skipped_dup,
                   cost_usd, queries_used, sources_discovered
              FROM paper_source_expansions
             ORDER BY run_date DESC, created_at DESC
             LIMIT 4
            """
        ),
        "recent_runs": _rows(
            """
            SELECT id, run_type, status, records_written, duration_ms, created_at
              FROM pipeline_runs
             ORDER BY created_at DESC
             LIMIT 10
            """
        ),
    }


def main() -> int:
    try:
        snap = build()
    except Exception:  # noqa: BLE001
        snap = {"error": traceback.format_exc()}
    json.dump(snap, sys.stdout, default=str, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
