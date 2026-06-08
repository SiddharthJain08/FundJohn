"""SP-6 — Open-Window Spread-Cost Study runner.

Loads max_hold long & short exits from PG, samples deterministically, pulls
SIP NBBO quotes via the Alpaca CLI, aggregates, and writes the verdict.

OPERATOR NOTE: Do NOT run the pull step yourself — it is slow and paced.
The operator invokes this script; the real CLI pull is the operator's step.

NO-PEEK: progress prints counts only; spread values are NEVER printed mid-run.
Spec: docs/superpowers/specs/2026-06-08-sp6-open-spread-cost-study-prereg.md
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402

from research.exit_timing import open_spread_study as s  # noqa: E402

# ── window definitions (from prereg §2, locked) ───────────────────────────────
_OPEN_WINDOW = ((9, 31, 0), (9, 32, 0))
_CLOSE_WINDOW = ((15, 55, 0), (15, 56, 0))

# ── Alpaca CLI path ───────────────────────────────────────────────────────────
_ALPACA_BIN = "/root/go/bin/alpaca"


# ── PG loading ────────────────────────────────────────────────────────────────

def _load_exits(uri: str, start_date: str, end_date: str) -> dict[str, list]:
    """Load DISTINCT (ticker, exit_date) for max_hold long & short exits."""
    import psycopg2

    sql = """
        SELECT DISTINCT t.ticker,
               t.exit_date::text AS exit_date,
               t.direction
        FROM strategy_backtest_trades t
        JOIN strategy_backtest_runs r ON r.run_id = t.run_id
        WHERE r.primary_window = TRUE
          AND t.exit_reason = 'max_hold'
          AND t.direction IN ('long', 'short')
          AND t.exit_date BETWEEN %(start)s AND %(end)s
    """
    conn = psycopg2.connect(uri)
    try:
        df = pd.read_sql(sql, conn, params={"start": start_date, "end": end_date})
    finally:
        conn.close()

    out: dict[str, list] = {"long": [], "short": []}
    for direction in ("long", "short"):
        sub = df[df["direction"] == direction]
        pairs = sorted(zip(sub["ticker"], sub["exit_date"]))
        out[direction] = pairs
    return out


# ── CLI quote pull ────────────────────────────────────────────────────────────

def _pull_quotes(symbol: str, start_iso: str, end_iso: str,
                 limit: int = 50) -> list[dict] | None:
    """Pull SIP NBBO quotes via Alpaca CLI. Returns list of quote dicts or None on failure.

    CLI: alpaca data quotes --symbol T --start S --end E --limit 50 --jq .quotes --quiet
    limit=50 raw quotes is enough to yield >=20 valid after dropping crossed/locked;
    stays on 1 page (no pagination). 50 raw >> K_QUOTES=20 valid needed.
    Returns a list (may be empty); None on any parse/exec failure after retries.
    """
    cmd = [
        _ALPACA_BIN, "data", "quotes",
        "--symbol", symbol,
        "--start", start_iso,
        "--end", end_iso,
        "--limit", str(limit),
        "--jq", ".quotes",
        "--quiet",
    ]

    backoffs = [1, 2, 4]
    for attempt, backoff in enumerate(backoffs + [None]):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ},
            )
            if result.returncode != 0:
                if attempt < len(backoffs):
                    time.sleep(backoff)
                    continue
                return None
            text = result.stdout.strip()
            if not text or text.lower() in ("null", "[]", ""):
                return []
            parsed = json.loads(text)
            if parsed is None:
                return []
            if not isinstance(parsed, list):
                return []
            return parsed
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            if attempt < len(backoffs):
                time.sleep(backoff)
                continue
            return None

    return None  # exhausted


# ── cache helpers ─────────────────────────────────────────────────────────────

def _load_cache(cache_path: pathlib.Path) -> set[str]:
    """Return set of already-processed keys 'ticker|date|direction'."""
    keys: set[str] = set()
    if not cache_path.exists():
        return keys
    with cache_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                keys.add(f"{rec['ticker']}|{rec['date']}|{rec['direction']}")
            except (json.JSONDecodeError, KeyError):
                continue
    return keys


def _append_cache(cache_path: pathlib.Path, record: dict) -> None:
    with cache_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _load_valid_events(cache_path: pathlib.Path) -> list[dict]:
    """Load all valid (both windows non-None) events from the cache."""
    events = []
    if not cache_path.exists():
        return events
    with cache_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("valid"):
                    events.append(rec)
            except json.JSONDecodeError:
                continue
    return events


# ── per-event spread computation ──────────────────────────────────────────────

def _compute_event(ticker: str, date_str: str, direction: str) -> dict:
    """Pull open & close windows, compute median half-spreads, return event dict."""
    open_start, open_end = s.et_window_utc(date_str, *_OPEN_WINDOW)
    close_start, close_end = s.et_window_utc(date_str, *_CLOSE_WINDOW)

    open_quotes = _pull_quotes(ticker, open_start, open_end)
    time.sleep(0.2)
    close_quotes = _pull_quotes(ticker, close_start, close_end)
    time.sleep(0.2)

    open_hs = s.window_median_half_spread(open_quotes) if open_quotes is not None else None
    close_hs = s.window_median_half_spread(close_quotes) if close_quotes is not None else None

    valid = open_hs is not None and close_hs is not None
    incr = (open_hs - close_hs) if valid else None

    return {
        "ticker": ticker,
        "date": date_str,
        "direction": direction,
        "open_hs": open_hs,
        "close_hs": close_hs,
        "incr": incr,
        "valid": valid,
    }


# ── report rendering ──────────────────────────────────────────────────────────

def render_report(agg: dict, v: dict) -> str:
    """Render the final analysis report as Markdown."""
    lines = []
    lines.append("# SP-6 — Open-Window Spread-Cost Study\n")
    lines.append("**Spec**: docs/superpowers/specs/2026-06-08-sp6-open-spread-cost-study-prereg.md\n")
    lines.append(
        "Quantity: incremental NBBO half-spread (open 09:31-09:32 ET vs close 15:55-15:56 ET) "
        "on max_hold exits. incr = open_hs − close_hs. "
        "net = gross_edge − incr.\n"
    )

    for direction in ("long", "short"):
        d = agg.get(direction, {})
        n = d.get("n", 0)
        lines.append(f"## {direction.title()} (n={n})\n")
        if n == 0:
            lines.append("_No valid events._\n")
            continue

        def _row(metric):
            return (
                f"| {metric} "
                f"| {d.get(f'{metric}_mean', float('nan')):.4f} "
                f"| {d.get(f'{metric}_median', float('nan')):.4f} "
                f"| {d.get(f'{metric}_p25', float('nan')):.4f} "
                f"| {d.get(f'{metric}_p75', float('nan')):.4f} "
                f"| {d.get(f'{metric}_p90', float('nan')):.4f} |"
            )

        lines.append("| metric | mean | median | p25 | p75 | p90 |")
        lines.append("|---|---|---|---|---|---|")
        for metric in ("open_hs", "close_hs", "incr"):
            lines.append(_row(metric))
        lines.append("")

    gross_long = s.LONG_GROSS_EDGE_BPS
    gross_short = s.SHORT_GROSS_EDGE_BPS
    lines.append("## Net edge (gross − incr)\n")
    lines.append(f"Gross edges (from Probe ①): LONG={gross_long} bps, SHORT={gross_short} bps\n")

    if v["label"] != "INVALID-DATA":
        lines.append("| leg | net_mean | net_median |")
        lines.append("|---|---|---|")
        lines.append(
            f"| long_net | {v['long_net_mean']:.4f} | {v['long_net_median']:.4f} |"
        )
        lines.append(
            f"| short_net | {v['short_net_mean']:.4f} | {v['short_net_median']:.4f} |"
        )
        lines.append("")

    lines.append(f"**VERDICT: {v['label']}**\n")
    lines.append(
        "Decision linkage: "
        "PARK -> open-exit channel closed with real cost data (spreads eat the only favorable leg); "
        "SHIP-SHORTS-CANDIDATE -> separate shorts-only live-lane design decision (NOT longs — "
        "long_net <= 0 by construction); "
        "MARGINAL -> operator call; likely not worth the 3-seam live build for sub-bp net. "
        "net = gross_edge − incremental_spread.\n"
    )
    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="SP-6 open-window spread-cost study runner. "
                    "DO NOT run in tests — mocks the CLI."
    )
    ap.add_argument("--analysis-dir", default="analysis")
    ap.add_argument("--start-date", default="2025-06-01")
    ap.add_argument("--end-date", default="2026-05-31")
    ap.add_argument("--limit-per-dir", type=int, default=s.SAMPLE_PER_DIR)
    ap.add_argument(
        "--cache", default="analysis/open_spread_study/pull_cache.jsonl"
    )
    args = ap.parse_args()

    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        print("[spread] POSTGRES_URI not set", flush=True)
        return 2

    # Output dir
    out_dir = pathlib.Path(args.analysis_dir) / "open_spread_study"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = pathlib.Path(args.cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Load population
    print("[spread] Loading exits from PG...", flush=True)
    exits = _load_exits(uri, args.start_date, args.end_date)
    for direction in ("long", "short"):
        print(
            f"[spread] {direction.upper()} population: "
            f"{len(exits[direction])} distinct (ticker,date)",
            flush=True,
        )

    # Sample
    sampled: dict[str, list] = {}
    for direction in ("long", "short"):
        sampled[direction] = s.deterministic_sample(exits[direction], args.limit_per_dir)
        print(
            f"[spread] {direction.upper()} sample: {len(sampled[direction])} events",
            flush=True,
        )

    # Load existing cache (keyed by ticker|date|direction)
    done_keys = _load_cache(cache_path)
    print(f"[spread] Cache: {len(done_keys)} events already pulled", flush=True)

    # Pull loop
    for direction in ("long", "short"):
        events_list = sampled[direction]
        n_total = len(events_list)
        n_valid = 0
        for i, (ticker, date_str) in enumerate(events_list):
            key = f"{ticker}|{date_str}|{direction}"
            if key in done_keys:
                continue
            record = _compute_event(ticker, date_str, direction)
            if record["valid"]:
                n_valid += 1
            _append_cache(cache_path, record)
            done_keys.add(key)
            if (i + 1) % 50 == 0 or i == n_total - 1:
                print(
                    f"[spread] {i+1}/{n_total} {direction} pulled (valid so far: {n_valid})",
                    flush=True,
                )

    # Aggregate and verdict
    all_events = _load_valid_events(cache_path)
    print(
        f"[spread] Total valid events in cache: {len(all_events)} "
        f"({sum(1 for e in all_events if e['direction']=='long')} long, "
        f"{sum(1 for e in all_events if e['direction']=='short')} short)",
        flush=True,
    )

    agg = s.aggregate(all_events)
    v = s.verdict(agg)

    # Write events.parquet
    if all_events:
        pd.DataFrame(all_events).to_parquet(out_dir / "events.parquet", index=False)
        print(f"[spread] events.parquet written ({len(all_events)} rows)", flush=True)

    # Write report.md
    md = render_report(agg, v)
    (out_dir / "report.md").write_text(md)
    print(md, flush=True)
    print(f"[spread] VERDICT: {v['label']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
