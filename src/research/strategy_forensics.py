#!/usr/bin/env python3
"""strategy_forensics.py — consolidated dossier for one strategy.

Answers questions like "why is X degrading?" by gathering in one shot:
  * registry row (status, backtest vs live sharpe, implementation path)
  * recent execution_signals + signal_pnl  (last 30 days)
  * upstream provenance via strategy_staging.source_paper_id →
    research_candidates.hunter_result_json, paper_gate_decisions,
    research_corpus abstract (for paper-derived strategies)
  * recent market_regime rows
  * strategy lifecycle events

Usage:
    python3 src/research/strategy_forensics.py <strategy_id> [--days N]

Prints JSON. MasterMindJohn calls this once per forensics query instead of
running 6 separate db-query calls — cheaper in Opus tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras

PG_URI = os.environ.get(
    "POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw"
)


def _rows(cur, sql: str, params: tuple) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _row(cur, sql: str, params: tuple) -> dict | None:
    cur.execute(sql, params)
    r = cur.fetchone()
    return dict(r) if r else None


def build_dossier(strategy_id: str, days: int = 30) -> dict:
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        registry = _row(
            cur,
            "SELECT id, name, status, tier, implementation_path, parameters, "
            "       regime_conditions, universe, signal_frequency, "
            "       backtest_sharpe, backtest_return_pct, backtest_max_dd_pct, "
            "       created_at, approved_at, deprecated_at, deprecation_reason "
            "  FROM strategy_registry WHERE id = %s",
            (strategy_id,),
        )

        # Recent signals + P&L
        signals = _rows(
            cur,
            """SELECT id::text, signal_date, ticker, direction, entry_price,
                      stop_loss, target_1, status, regime_state,
                      confluence_count, created_at
                 FROM execution_signals
                WHERE strategy_id = %s
                  AND signal_date >= CURRENT_DATE - %s::int
                ORDER BY signal_date DESC
                LIMIT 50""",
            (strategy_id, days),
        )
        pnl = _rows(
            cur,
            """SELECT pnl_date, close_price, unrealized_pnl_pct, days_held,
                      status, close_reason, closed_at
                 FROM signal_pnl
                WHERE strategy_id = %s
                  AND pnl_date >= CURRENT_DATE - %s::int
                ORDER BY pnl_date DESC
                LIMIT 200""",
            (strategy_id, days),
        )

        # Aggregate realised P&L summary
        pnl_summary = _row(
            cur,
            """SELECT COUNT(*)::int AS n_rows,
                      COUNT(*) FILTER (WHERE status='closed')::int AS n_closed,
                      AVG(realized_pnl_pct) FILTER (WHERE status='closed') AS avg_closed_pct,
                      AVG(days_held) FILTER (WHERE status='closed') AS avg_hold_days,
                      COUNT(*) FILTER (WHERE close_reason='stop_loss')::int AS stops_hit
                 FROM signal_pnl
                WHERE strategy_id = %s
                  AND pnl_date >= CURRENT_DATE - %s::int""",
            (strategy_id, days),
        )

        # Upstream provenance chain: staging → candidate → paper (if any)
        staging = _row(
            cur,
            """SELECT id::text, proposed_by, source_session_id::text,
                      source_paper_id, name, thesis, parameters, status,
                      quick_backtest_json, created_at, decided_at
                 FROM strategy_staging
                WHERE promoted_strategy_id = %s
                   OR name = %s
                ORDER BY created_at DESC
                LIMIT 1""",
            (strategy_id, strategy_id),
        )

        candidate = None
        gate_decisions: list[dict] = []
        paper = None
        if staging and staging.get("source_paper_id"):
            candidate_id = staging["source_paper_id"]
            candidate = _row(
                cur,
                """SELECT candidate_id::text, source_url, submitted_by, kind,
                          priority, hunter_result_json, submitted_at
                     FROM research_candidates
                    WHERE candidate_id::text = %s""",
                (candidate_id,),
            )
            gate_decisions = _rows(
                cur,
                """SELECT gate_name, outcome, reason_code, reason_detail,
                          occurred_at, metadata
                     FROM paper_gate_decisions
                    WHERE candidate_id::text = %s
                    ORDER BY occurred_at ASC""",
                (candidate_id,),
            )
            if candidate and candidate.get("source_url"):
                paper = _row(
                    cur,
                    """SELECT paper_id::text, title, abstract, authors, venue,
                              published_date, source
                         FROM research_corpus
                        WHERE source_url = %s""",
                    (candidate["source_url"],),
                )

        # Market regime snapshot
        regime = _rows(
            cur,
            """SELECT updated_at, state, vix_level, vix_percentile, regime_data
                 FROM market_regime
                ORDER BY updated_at DESC
                LIMIT 10""",
            (),
        )

        # Try to find strategy lifecycle events (best-effort — table may not exist)
        lifecycle: list[dict] = []
        try:
            lifecycle = _rows(
                cur,
                """SELECT created_at, event_type, old_state, new_state, reason
                     FROM strategy_lifecycle_events
                    WHERE strategy_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20""",
                (strategy_id,),
            )
        except Exception:
            conn.rollback()

    # Heuristic degradation signals — shortcut for MMJ's first pass
    flags: list[str] = []
    if registry:
        bs = registry.get("backtest_sharpe")
        if bs is not None and pnl_summary and pnl_summary.get("avg_closed_pct") is not None:
            avg_pct = float(pnl_summary["avg_closed_pct"])
            if bs > 0.5 and avg_pct < 0:
                flags.append(f"avg closed P&L {avg_pct:+.2f}% while backtest sharpe was {bs:+.2f}")
        if pnl_summary and pnl_summary.get("stops_hit") and pnl_summary.get("n_closed"):
            stop_rate = pnl_summary["stops_hit"] / max(1, pnl_summary["n_closed"])
            if stop_rate > 0.5:
                flags.append(f"{stop_rate*100:.0f}% of closed signals hit stop")
    if regime:
        states = [r.get("state") for r in regime[:5]]
        if registry and registry.get("regime_conditions"):
            allowed = list((registry["regime_conditions"] or {}).keys())
            if allowed and all(s not in allowed for s in states if s):
                flags.append(
                    f"recent regime states {states} outside strategy's active set {allowed}"
                )

    return {
        "strategy_id":  strategy_id,
        "days_window":  days,
        "registry":     registry,
        "pnl_summary":  pnl_summary,
        "signals":      signals,
        "pnl_recent":   pnl,
        "staging":      staging,
        "candidate":    candidate,
        "gate_decisions": gate_decisions,
        "paper":        paper,
        "regime":       regime,
        "lifecycle":    lifecycle,
        "degradation_flags": flags,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("strategy_id")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    dossier = build_dossier(args.strategy_id, args.days)
    print(json.dumps(dossier, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
