"""SP-6 B-flow Phase 1 — driver: extract -> fetch -> compute -> artifact -> report.

Ties together the three pure/IO modules of the kill-test (spec §6):

  order_set      -> historical order intents (both grains) + worked-session
                    resolution + per-session regime fallback (READ-ONLY DB).
  minbar_cache   -> Alpaca 1-min bars per worked session (rebuildable parquet
                    cache; fetch is niced/sequential at the call site).
  oracle         -> per-intent oracle/benchmark math (PURE; no I/O).

This module is the only place the three meet. It:

  * pre-attaches each intent's regime (own regime_state, else session fallback),
  * evaluates every intent against its (worked_session, ticker) bars,
  * aggregates the NULL-RELATIVE excess (directed - sign-flipped) — the headline
    everywhere — and applies the pre-committed kill criterion on the PRIMARY
    grain, equal-weight (spec §6.3),
  * writes the parquet artifact + the markdown report under
    ``$BFLOW_ANALYSIS_DIR`` (default /root/openclaw/analysis) — NOT the worktree,
    NOT data/master,
  * optionally posts a truncated summary to #data-alerts.

Doctrine (locked): the directed prize is reported only as the "range-adequacy
bound (not capturable)"; the null-relative excess is the headline. Two EOD
benchmarks, named, never conflated: P_eod_dump = "beats our current execution",
P_eod_close = the validated-backtest parity anchor.

Determinism: no randomness; every dict/set iteration is sorted so the report
text and the parquet row order are stable run-to-run.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from src.research.bflow import minbar_cache, oracle, order_set

# Default artifact directory — overridable via env so tests stay hermetic.
_DEFAULT_ANALYSIS_DIR = "/root/openclaw/analysis"

# Oracle-time histogram buckets (window start minute, offset from 09:30 ET).
# Open / early-mid / mid / pre-close / dump.
_HIST_BUCKETS = (
    ("0-29", 0, 29),
    ("30-89", 30, 89),
    ("90-329", 90, 329),
    ("330-374", 330, 374),
    ("375-389", 375, 389),
)

# Parquet column order — kept stable so re-runs produce byte-comparable schema.
_ARTIFACT_COLUMNS = [
    "grain", "worked_session", "ticker", "direction", "regime",
    "n_signals", "sum_size_pct", "qty", "filled_avg_price", "strategy_id",
    "p_eod_dump", "p_eod_close",
    "strict_price", "strict_minute", "strict_gross_bps", "strict_net_bps",
    "win_price", "win_start_minute", "win_gross_bps", "win_net_bps",
    "flip_win_price", "flip_win_start_minute", "flip_win_gross_bps",
    "flip_win_net_bps", "excess_net_bps",
    "directed_in_last15", "flipped_in_last15",
    "n_valid_bars", "exclude_reason",
]


# --------------------------------------------------------------------------
# weighted percentile (single hand-computable convention used everywhere)
# --------------------------------------------------------------------------
def weighted_percentile(values, weights, q):
    """Return the value at quantile ``q`` (0..1) under ``weights``.

    Convention (deliberately NON-interpolating so it always returns an actual
    observed value and equal-weight matches per_session_median's lower-median):
    sort (value, weight) by value, accumulate weight, return the first value
    whose cumulative weight >= q * total_weight. None if empty / no weight.

    Equal-weight = pass weights of 1.0 each.
    """
    pairs = sorted(
        (float(v), float(w))
        for v, w in zip(values, weights)
        if v is not None and w is not None and (float(w) > 0)
    )
    total = sum(w for _, w in pairs)
    if not pairs or not (total > 0):
        return None
    target = q * total
    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= target:
            return v
    return pairs[-1][0]


def _weighted_stats(values, weights):
    """median / p25 / p75 under ``weights`` + count of contributing rows."""
    return {
        "median": weighted_percentile(values, weights, 0.50),
        "p25": weighted_percentile(values, weights, 0.25),
        "p75": weighted_percentile(values, weights, 0.75),
        "n": sum(1 for v in values if v is not None),
    }


# --------------------------------------------------------------------------
# row construction
# --------------------------------------------------------------------------
def _excluded_row(intent, reason):
    """Build an excluded row with the SAME key set as an included one (oracle
    metric fields all None) so the parquet schema is stable across rows."""
    rec = oracle.evaluate_intent([], intent.get("direction", "LONG"))
    rec["exclude_reason"] = reason
    return {**intent, **rec}


def evaluate_all(intents, bars_by_ticker_loader, window=15):
    """Evaluate every intent against its (worked_session, ticker) bars.

    ``bars_by_ticker_loader`` is ``loader(session) -> {ticker: [Bar, ...]}``;
    it is called ONCE per distinct worked_session (so batch fetching is not
    defeated). Each intent is merged with oracle.evaluate_intent's output. An
    intent whose ticker has no bars (missing / empty) becomes a row with
    ``exclude_reason='no_bars'`` (intent fields kept, metric fields None) so it
    is COUNTED in the data-quality table, never silently dropped.

    Rows are returned grouped by session in the input order of sessions, and
    within a session sorted by (ticker, direction) for deterministic output.
    """
    by_session = {}
    for it in intents:
        by_session.setdefault(it["worked_session"], []).append(it)

    rows = []
    for session in sorted(by_session):
        loaded = bars_by_ticker_loader(session) or {}
        group = sorted(by_session[session],
                       key=lambda x: (x["ticker"], str(x.get("direction"))))
        for it in group:
            bars = loaded.get(it["ticker"]) or []
            if not bars:
                rows.append(_excluded_row(it, "no_bars"))
                continue
            rec = oracle.evaluate_intent(bars, it["direction"], window=window)
            rows.append({**it, **rec})
    return rows


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
def _included(rows, grain=None):
    out = [r for r in rows if r.get("exclude_reason") is None]
    if grain is not None:
        out = [r for r in out if r.get("grain") == grain]
    return out


def per_session_medians(rows, field="excess_net_bps"):
    """dict session -> median of ``field`` over INCLUDED rows only
    (exclude_reason is None). Uses oracle.per_session_median (lower-median)."""
    by_session = {}
    for r in _included(rows):
        by_session.setdefault(r["worked_session"], []).append(r.get(field))
    return {
        s: oracle.per_session_median(vals)
        for s, vals in sorted(by_session.items())
        if oracle.per_session_median(vals) is not None
    }


def _conviction_weights(rows):
    """Σ size_pct per row (primary conviction weight); 0 -> treated as absent."""
    return [float(r.get("sum_size_pct") or 0.0) for r in rows]


def _dollar_weights(rows):
    """|qty| * filled_avg_price per row (broker $-weight)."""
    out = []
    for r in rows:
        q = r.get("qty")
        p = r.get("filled_avg_price")
        if q is None or p is None:
            out.append(0.0)
        else:
            out.append(abs(float(q)) * float(p))
    return out


def _headline_primary(rows):
    inc = _included(rows, grain="primary")
    vals = [r.get("excess_net_bps") for r in inc]
    eq = _weighted_stats(vals, [1.0] * len(inc))
    cw = _weighted_stats(vals, _conviction_weights(inc))
    return {"equal_weight": eq, "conviction_weight": cw}


def _headline_broker(rows):
    inc = _included(rows, grain="broker")
    vals = [r.get("excess_net_bps") for r in inc]
    eq = _weighted_stats(vals, [1.0] * len(inc))
    dw = _weighted_stats(vals, _dollar_weights(inc))
    return {"equal_weight": eq, "dollar_weight": dw}


def _oracle_time_histogram(rows):
    inc = _included(rows, grain="primary")
    hist = {label: 0 for label, _, _ in _HIST_BUCKETS}
    for r in inc:
        m = r.get("win_start_minute")
        if m is None:
            continue
        for label, lo, hi in _HIST_BUCKETS:
            if lo <= m <= hi:
                hist[label] += 1
                break
    return hist


def _oracle_eq_eod(rows):
    """oracle≈EOD frequency, reported DIRECTED and FLIPPED, each time-based
    (window starts in the last 15 RTH minutes) and value-based (net excess <= 0,
    NaN-safe via ``not (x > 0)``)."""
    inc = _included(rows, grain="primary")
    n = len(inc)
    d_time = sum(1 for r in inc if r.get("directed_in_last15"))
    f_time = sum(1 for r in inc if r.get("flipped_in_last15"))
    # value-based (§5): directed net alpha <= 0 ("waiting was the right policy").
    # Uses the DIRECTED net (win_net_bps), NOT the null-relative excess — both
    # directed and flipped are reported on their own raw net so the difference
    # is the direction-conditioned part. NaN-safe via `not (x > 0)`.
    d_value = sum(1 for r in inc if not (oracle._f(r.get("win_net_bps")) > 0))
    f_value = sum(1 for r in inc if not (oracle._f(r.get("flip_win_net_bps")) > 0))
    return {
        "n": n,
        "directed": {"time_based": d_time, "value_based": d_value},
        "flipped": {"time_based": f_time, "value_based": f_value},
    }


def _split_stats(rows, key):
    """Group INCLUDED primary rows by ``key`` -> equal-weight excess stats."""
    inc = _included(rows, grain="primary")
    groups = {}
    for r in inc:
        k = r.get(key)
        groups.setdefault(k, []).append(r.get("excess_net_bps"))
    out = {}
    for k in sorted(groups, key=lambda x: ("" if x is None else str(x))):
        vals = groups[k]
        out[k] = _weighted_stats(vals, [1.0] * len(vals))
    return out


def _long_short(rows):
    return {str(k).upper(): v
            for k, v in _split_stats(rows, "direction").items()
            if k is not None}


def _exclusion_counts(rows):
    counts = {}
    for r in rows:
        reason = r.get("exclude_reason")
        if reason is not None:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _divergence(rows, closes):
    """p_eod_close vs p_eod_dump divergence + (optional) vs parquet close.

    Over INCLUDED rows only (excluded rows null p_eod_dump/p_eod_close even when
    a dump window existed — contract gotcha #1). ``closes`` maps
    (worked_session, ticker) -> parquet close; rows with no entry are skipped in
    the parquet comparison. Divergence >50bps is flagged (kept in, reported).
    """
    inc = _included(rows)
    # dump-vs-close divergence (always available on included rows)
    dump_close = []
    for r in inc:
        dump = oracle._f(r.get("p_eod_dump"))
        close = oracle._f(r.get("p_eod_close"))
        if (dump > 0) and (close > 0):
            dump_close.append(abs(close - dump) / dump * 1e4)

    n_compared = 0
    n_flagged = 0
    parquet_div = []
    if closes:
        for r in inc:
            key = (r.get("worked_session"), r.get("ticker"))
            if key not in closes:
                continue
            pclose = oracle._f(closes[key])
            oclose = oracle._f(r.get("p_eod_close"))
            if not (pclose > 0) or not (oclose > 0):
                continue
            div = abs(oclose - pclose) / pclose * 1e4
            parquet_div.append(div)
            n_compared += 1
            if div > 50.0:
                n_flagged += 1

    return {
        "dump_vs_close_median_bps": oracle.per_session_median(dump_close),
        "n_compared": n_compared,
        "n_flagged_gt50bps": n_flagged,
        "parquet_div_median_bps": oracle.per_session_median(parquet_div),
    }


def aggregate_report(rows, closes=None):
    """Build the full aggregate dict consumed by format_report.

    Headline = null-relative excess (excess_net_bps) for primary (equal +
    conviction weight) and broker ($-weight); range-adequacy bound = raw
    directed win_net_bps median (labeled not capturable); per-session medians;
    kill_verdict on the PRIMARY grain, equal-weight; oracle-time histogram;
    oracle≈EOD directed AND flipped; LONG/SHORT + per-regime splits; exclusion
    counts; divergence stats.
    """
    # Kill criterion is PRIMARY grain, equal-weight (spec §6.3 / contract #5):
    # the public per_session_medians is grain-agnostic, so filter here to keep
    # broker excess out of the GO/KILL verdict (and out of the per-session
    # table the report renders).
    primary_rows = [r for r in rows if r.get("grain") == "primary"]
    psm = per_session_medians(primary_rows, field="excess_net_bps")
    verdict = oracle.kill_verdict(psm)

    inc_primary = _included(rows, grain="primary")
    ra_vals = [r.get("win_net_bps") for r in inc_primary]
    range_adequacy = _weighted_stats(ra_vals, [1.0] * len(inc_primary))
    range_adequacy["not_capturable"] = True

    return {
        "n_rows": len(rows),
        "n_included_primary": len(inc_primary),
        "n_included_broker": len(_included(rows, grain="broker")),
        "headline": {
            "primary": _headline_primary(rows),
            "broker": _headline_broker(rows),
        },
        "range_adequacy_bound": range_adequacy,
        "per_session_medians": psm,
        "kill_verdict": verdict,
        "oracle_time_histogram": _oracle_time_histogram(rows),
        "oracle_eq_eod": _oracle_eq_eod(rows),
        "long_short": _long_short(rows),
        "per_regime": _split_stats(rows, "regime"),
        "exclusions": _exclusion_counts(rows),
        "divergence": _divergence(rows, closes),
    }


# --------------------------------------------------------------------------
# report rendering
# --------------------------------------------------------------------------
def _fmt(x, suffix=""):
    if x is None:
        return "n/a"
    return f"{x:+.2f}{suffix}"


def _stats_line(label, stats):
    return (f"- {label}: median {_fmt(stats.get('median'))} "
            f"[P25 {_fmt(stats.get('p25'))}, P75 {_fmt(stats.get('p75'))}] "
            f"(n={stats.get('n', 0)})")


def format_report(agg):
    """Render the spec §6 markdown report. Load-bearing strings (asserted by the
    spec/tests): 'not capturable', 'beats our current execution', 'beats the
    validated backtest parity anchor', the directed-vs-flipped section, the kill
    verdict line, the DTBP/PDT note."""
    v = agg["kill_verdict"]
    verdict_word = "GO" if v.get("go") else "KILL"
    L = []
    L.append("# SP-6 B-flow Phase 1 — Oracle Kill-Test Report")
    L.append("")
    L.append(f"**VERDICT: {verdict_word}** — across-session median of "
             f"per-session median net excess = "
             f"{_fmt(v.get('median_of_medians'), 'bps')} (GO bar > +2.00bps), "
             f"positive-session fraction = "
             f"{(v.get('frac_pos_sessions') or 0.0):.0%} (GO bar >= 60%), "
             f"n_sessions = {v.get('n_sessions', 0)}.")
    L.append("")

    # Headline — null-relative excess (the only headline doctrine)
    L.append("## Headline — null-relative excess (directed - sign-flipped, net, "
             "window oracle)")
    L.append("The achievable window-oracle prize NET of differential cost, "
             "MINUS the same prize computed with the opposite direction on the "
             "same bars. E[excess]=0 under the no-directional-structure null, so "
             "this is what a causal engine could in principle capture; the raw "
             "directed prize is volatility harvest, NOT a signal.")
    hp = agg["headline"]["primary"]
    L.append("")
    L.append("### Primary grain (signal grain)")
    L.append(_stats_line("equal-weight", hp["equal_weight"]))
    L.append(_stats_line("conviction-weight (Σ size_pct)", hp["conviction_weight"]))
    hb = agg["headline"]["broker"]
    L.append("")
    L.append("### Broker grain ($-weighted realism slice)")
    L.append(_stats_line("equal-weight", hb["equal_weight"]))
    L.append(_stats_line("$-weight (|qty|*filled_avg_price)", hb["dollar_weight"]))
    L.append("")

    # Range-adequacy bound — explicitly NOT capturable
    ra = agg["range_adequacy_bound"]
    L.append("## Range-adequacy bound (not capturable)")
    L.append("Raw directed window-oracle prize (median win_net_bps). This is the "
             "volatility-harvest upper bound, reported for context only — it is "
             "**not capturable** signal and is never the acceptance bar.")
    L.append(_stats_line("raw directed prize", ra))
    L.append("")

    # Dual benchmark naming — named, never conflated
    L.append("## Benchmarks (named, never conflated)")
    L.append("- **P_eod_dump** (final-5-min VWAP, the 3:55 dump) -> the headline "
             "measures whether the oracle **beats our current execution**.")
    L.append("- **P_eod_close** (last RTH minute close, cross-checked vs "
             "prices.parquet) -> the **validated backtest parity anchor**; "
             "clearing it means the oracle **beats the validated backtest parity "
             "anchor**.")
    L.append("")

    # oracle≈EOD — directed AND flipped, side by side
    oe = agg["oracle_eq_eod"]
    n = oe.get("n", 0) or 1
    L.append("## oracle≈EOD frequency — directed vs flipped (sign-flipped)")
    L.append("Reported BOTH ways: the raw frequency is mechanically high under "
             "extreme-value bias and must not be read alone — the directed-minus-"
             "flipped difference is the direction-conditioned part.")
    L.append(f"- directed: time-based "
             f"{oe['directed']['time_based']}/{oe['n']} "
             f"({oe['directed']['time_based']/n:.0%}), value-based "
             f"{oe['directed']['value_based']}/{oe['n']} "
             f"({oe['directed']['value_based']/n:.0%})")
    L.append(f"- flipped:  time-based "
             f"{oe['flipped']['time_based']}/{oe['n']} "
             f"({oe['flipped']['time_based']/n:.0%}), value-based "
             f"{oe['flipped']['value_based']}/{oe['n']} "
             f"({oe['flipped']['value_based']/n:.0%})")
    L.append("")

    # oracle-time histogram
    L.append("## Oracle-time histogram (window start, minutes from 09:30)")
    for label, _, _ in _HIST_BUCKETS:
        L.append(f"- {label}: {agg['oracle_time_histogram'][label]}")
    L.append("")

    # LONG vs SHORT
    L.append("## LONG vs SHORT (equal-weight excess)")
    for side in ("LONG", "SHORT"):
        if side in agg["long_short"]:
            L.append(_stats_line(side, agg["long_short"][side]))
    L.append("")

    # per-regime
    L.append("## Per-regime split (equal-weight excess)")
    for regime, stats in agg["per_regime"].items():
        L.append(_stats_line(str(regime), stats))
    L.append("")

    # per-session table
    L.append("## Per-session median net excess")
    for session, med in sorted(agg["per_session_medians"].items()):
        L.append(f"- {session}: {_fmt(med, 'bps')}")
    L.append("")

    # data-quality table
    dv = agg["divergence"]
    L.append("## Data-quality table")
    L.append(f"- rows total: {agg['n_rows']}  |  included primary: "
             f"{agg['n_included_primary']}  |  included broker: "
             f"{agg['n_included_broker']}")
    L.append("- excluded by reason: " + (
        ", ".join(f"{k}={v}" for k, v in agg["exclusions"].items())
        or "none"))
    L.append(f"- P_eod_dump vs P_eod_close divergence (median): "
             f"{_fmt(dv['dump_vs_close_median_bps'], 'bps')}")
    L.append(f"- minute-close vs prices.parquet close: compared "
             f"{dv['n_compared']}, median "
             f"{_fmt(dv['parquet_div_median_bps'], 'bps')}, "
             f">50bps flagged {dv['n_flagged_gt50bps']}")
    L.append("")

    # DTBP/PDT context note (context, not modeled in Phase 1)
    L.append("## DTBP/PDT note (context, not modeled in Phase 1)")
    L.append("Early entries plus same-day stop/TP exits book day-trades the 3:55 "
             "dump avoids — DTBP/PDT constraints mean realized capture in a later "
             "phase is capped BELOW this prize. Not modeled here; modeled in the "
             "Phase 2 eval.")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Discord post (b1_ledger.post_report pattern)
# --------------------------------------------------------------------------
def post_discord(text, channel="data-alerts"):
    """Post ``text`` to the #data-alerts webhook (agent_registry.webhook_urls).
    Truncates to 1900 chars with a pointer to the full report. Explicit
    User-Agent (Cloudflare 1010 rule). Returns True on a 2xx, else False."""
    import requests

    conn = order_set._conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_urls->>%s FROM agent_registry WHERE id='botjohn'",
            (channel,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return False

    suffix = "\n(full report: analysis/bflow_phase1_report.md)"
    if len(text) > 1900:
        text = text[:1900 - len(suffix)] + suffix
    try:
        r = requests.post(
            row[0], json={"content": text}, timeout=10,
            headers={"User-Agent": "OpenClaw-BflowPhase1/1.0 (+botjohn)"})
        return 200 <= r.status_code < 300
    except Exception:
        return False


# --------------------------------------------------------------------------
# artifact write
# --------------------------------------------------------------------------
def _analysis_dir():
    return os.environ.get("BFLOW_ANALYSIS_DIR", _DEFAULT_ANALYSIS_DIR)


def write_artifacts(rows, report_md, analysis_dir=None):
    """Write the parquet artifact + the markdown report. Returns
    (parquet_path, report_path). Creates the directory if needed."""
    import pandas as pd

    if analysis_dir is None:
        analysis_dir = _analysis_dir()
    os.makedirs(analysis_dir, exist_ok=True)

    parquet_path = os.path.join(analysis_dir, "bflow_phase1_oracle_orders.parquet")
    report_path = os.path.join(analysis_dir, "bflow_phase1_report.md")

    df = pd.DataFrame(
        [{c: r.get(c) for c in _ARTIFACT_COLUMNS} for r in rows],
        columns=_ARTIFACT_COLUMNS)
    df.to_parquet(parquet_path, index=False)

    with open(report_path, "w") as fh:
        fh.write(report_md)

    return parquet_path, report_path


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def _no_fetch_fetcher(missing, session):
    """Cache-only fetcher: never hits the network, returns no bars for any
    missing ticker (so an uncached ticker yields a counted no_bars row)."""
    return {s: [] for s in missing}


def _build_default_loader(session_tickers, no_fetch):
    """Return loader(session) -> {ticker: [Bar]} using minbar_cache. Only the
    tickers needed on each session (from ``session_tickers``) are requested."""
    fetcher = _no_fetch_fetcher if no_fetch else minbar_cache.fetch_session_bars

    def loader(session):
        tickers = sorted(session_tickers.get(session, set()))
        if not tickers:
            return {}
        return minbar_cache.get_session_bars(session, tickers, fetcher=fetcher)

    return loader


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="run_phase1")
    p.add_argument("--grain", choices=["primary", "broker", "both"],
                   default="both")
    p.add_argument("--window", type=int, default=15)
    p.add_argument("--limit", type=int, default=None,
                   help="cap intents per grain (smoke)")
    p.add_argument("--no-fetch", action="store_true",
                   help="cache-only; never hit the network")
    p.add_argument("--post", action="store_true",
                   help="post the summary to #data-alerts (default OFF)")
    return p.parse_args(argv)


def main(argv=None, conn=None, loader=None, sessions=None, today=None,
         closes=None):
    """CLI entry. Injection seams (conn/loader/sessions/today/closes) keep the
    smoke test hermetic; defaults build the real ones.

    Flow: load SPY sessions -> extract intents (selected grains) -> pre-attach
    regime -> for each needed session load bars (niced/sequential fetch) ->
    evaluate_all -> aggregate -> write artifacts -> print report -> optional post.
    Returns a process exit code (0 = success).
    """
    args = _parse_args([] if argv is None else argv)

    if today is None:
        from datetime import date
        today = date.today().isoformat()

    own_conn = conn is None
    if own_conn:
        conn = order_set._conn()
    try:
        if sessions is None:
            sessions = order_set.load_spy_sessions()

        intents = []
        if args.grain in ("primary", "both"):
            prim, prim_excl = order_set.primary_intents(today, sessions, conn=conn)
            if args.limit is not None:
                prim = prim[:args.limit]
            intents.extend(prim)
        if args.grain in ("broker", "both"):
            brok, brok_excl = order_set.broker_intents(today, conn=conn)
            if args.limit is not None:
                brok = brok[:args.limit]
            intents.extend(brok)

        # pre-attach regime (own regime_state if truthy, else session fallback)
        session_map = order_set.session_regime_map(conn=conn)
        for it in intents:
            it["regime"] = order_set.regime_for(it, session_map)
    finally:
        if own_conn:
            conn.close()

    # build the per-session ticker map for the fetch envelope log + loader
    session_tickers = {}
    for it in intents:
        session_tickers.setdefault(it["worked_session"], set()).add(it["ticker"])

    pairs = [(s, t) for s, ts in session_tickers.items() for t in ts]
    n_req = minbar_cache.estimate_requests(pairs)
    print(f"[bflow-p1] {len(intents)} intents over "
          f"{len(session_tickers)} sessions; ~{n_req} requests total "
          f"(no_fetch={args.no_fetch})")

    if loader is None:
        loader = _build_default_loader(session_tickers, args.no_fetch)

    rows = evaluate_all(intents, loader, window=args.window)
    agg = aggregate_report(rows, closes=closes)
    report_md = format_report(agg)

    parquet_path, report_path = write_artifacts(rows, report_md)
    print(report_md)
    print(f"[bflow-p1] wrote {parquet_path}")
    print(f"[bflow-p1] wrote {report_path}")

    if args.post:
        posted = post_discord(report_md)
        print(f"[bflow-p1] discord post: {'ok' if posted else 'skipped/failed'}")

    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
