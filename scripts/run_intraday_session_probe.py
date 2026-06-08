"""Probe ① runner. Loads max_hold-long exits + prices + regimes, calls
compute_probe, writes analysis/exit_timing_probe/{report.md,rows.parquet}.

NO-PEEK: progress prints counts only; the verdict block is the first look.
Spec: docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md
"""
from __future__ import annotations

import argparse
import os
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402
from research.exit_timing import intraday_session_probe as p  # noqa: E402


def load_primary_exits(conn) -> pd.DataFrame:
    sql = """
        SELECT DISTINCT t.ticker, t.exit_date
        FROM strategy_backtest_trades t
        JOIN strategy_backtest_runs r ON r.run_id = t.run_id
        WHERE r.primary_window = TRUE
          AND t.exit_reason = 'max_hold'
          AND t.direction = 'long'
    """
    df = pd.read_sql(sql, conn)
    df["date"] = df["exit_date"].astype(str)
    return df[["ticker", "date"]]


def _fmt_row(r) -> str:
    t = r.get("t")
    tt = "nan" if t != t else f"{t:+.3f}"  # noqa: PLR0124 (NaN check)
    return f"| {r.get('regime', r.get('bucket', ''))} | {r['mean']*1e4:+.3f} | {tt} | {r['n']} |"


def render_report(res: dict) -> str:
    pm = res["primary_m1"]; sm = res["secondary_m1"]; m2 = res["m2_relative"]
    def line(name, d):
        t = d["t"]; tt = "nan" if t != t else f"{t:+.4f}"
        return f"- {name}: mean {d['mean']*1e4:+.4f} bps | t {tt} | n_days {d['n']}"
    out = []
    out.append("# Probe ① — Intraday-Session Return (longs-only open-exit gate)\n")
    out.append("**Spec**: docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md\n")
    out.append("Quantity: intraday_return=(close-open)/open on max_hold-LONG exit days. "
               "Open-exit edge for a long = -E[intraday_return]. Asymmetric veto.\n")
    out.append("## Headline (day-clustered)\n")
    out.append(line("PRIMARY (max_hold-long)", pm))
    out.append(line("SECONDARY (equity universe)", sm))
    out.append(line("M2 relative (PRIMARY - same-day universe mean)", m2))
    out.append(f"- PRIMARY rows (ticker x exit_date): {res['n_primary_rows']}\n")
    out.append("## By regime (PRIMARY)\n")
    out.append("| regime | mean bps | t | n_days |")
    out.append("|---|---|---|---|")
    for r in res["by_regime"]:
        out.append(_fmt_row(r))
    out.append("\n## By half-year (PRIMARY)\n")
    out.append("| bucket | mean bps | t | n_days |")
    out.append("|---|---|---|---|")
    for r in res["by_halfyear"]:
        out.append(_fmt_row(r))
    out.append("")
    out.append("Decision rule (spec §1.4): NO-GO iff PRIMARY pooled t>=+3.0, OR any of the two "
               "most-recent half-years t>=+2.0. Else CLEAR (CAUTION if pooled mean>0). "
               "INVALID-DATA iff n_days<500.\n")
    out.append(f"**VERDICT: {res['verdict']}**\n")
    out.append("Decision linkage: NO-GO -> close-exit stands for longs, question closed. "
               "CLEAR(-WITH-CAUTION) -> proceed to the gated live-structure spec/plan "
               "(longs-only open-exit, >=9:31 marketable-limit/TIF=day + close fallback, "
               "forward-confirm on live fills). Net cost ratified by live fills only.\n")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis-dir", default="analysis")
    ap.add_argument("--prices", default="data/master/prices.parquet")
    ap.add_argument("--regimes", default="data/master/historical_regimes.parquet")
    args = ap.parse_args()

    import psycopg2
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        print("[exit-probe] POSTGRES_URI not set", flush=True)
        return 2
    conn = psycopg2.connect(uri)
    try:
        primary = load_primary_exits(conn)
    finally:
        conn.close()
    print(f"[exit-probe] PRIMARY exits loaded: {len(primary)} rows", flush=True)

    prices = pd.read_parquet(args.prices, columns=["ticker", "date", "open", "close"])
    print(f"[exit-probe] price rows: {len(prices)}", flush=True)
    regimes = pd.read_parquet(args.regimes, columns=["date", "regime"])
    print(f"[exit-probe] regime rows: {len(regimes)}", flush=True)

    res = p.compute_probe(primary, prices, regimes)

    out_dir = pathlib.Path(args.analysis_dir) / "exit_timing_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(res)
    (out_dir / "report.md").write_text(md)

    # rows.parquet: the PRIMARY attached frame for audit
    prepped = p.prep_prices(prices)
    prim = p.attach_regime_bucket(p.attach_primary(primary, prepped), regimes)
    prim.to_parquet(out_dir / "rows.parquet", index=False)

    print(md, flush=True)
    print(f"[exit-probe] VERDICT: {res['verdict']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
