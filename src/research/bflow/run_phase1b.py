"""SP-6 B-flow Phase 1b — runner: cache -> Test A (gating) + Test B (supportive)
-> verdict -> artifacts (spec §5/§6).

Ties together the pure modules of the predictability kill-test:

  minbar_cache    -> the rebuildable 1-min bar cache (READ-ONLY here: this runner
                     is CACHE-ONLY — it NEVER fetches; an absent session file is
                     counted in data-quality and skipped).
  flow_features   -> §3 features / forward targets (PURE).
  predictability  -> Test A: per-session pooled Spearman rank ICs + the
                     clustered across-session t (the GATING leg).
  flow_policy     -> Test B: the causal arm/trigger proxy policy (SUPPORTIVE,
                     NOT gating); run for BOTH the reversion and momentum
                     variants.
  oracle          -> dump_benchmark / dump-window spread (PURE), reused for the
                     Test-A dump prices and the Test-B differential cost.

This module is the only place they meet. It:

  * Test A universe = every (ticker, session) in the cache passing the Phase-1
    60-valid-bar floor; computes per-ticker dump benchmarks via
    ``oracle.dump_benchmark`` and runs the full 15-cell IC grid + summary.
  * Test B universe = the Phase-1 primary-grain included intents
    (grain=='primary' AND exclude_reason is null; 3,867 on real data — asserted
    and logged, never hard-failed in tests); joins each intent to cached bars,
    reuses the parquet's ``p_eod_dump``, and runs ``simulate_intent`` for both
    variants.
  * applies the PRE-COMMITTED §5 verdict (a pure, unit-testable function).
  * writes ``bflow_phase1b_report.md`` + ``bflow_phase1b_ic_grid.parquet``
    (sessions × cells) + ``bflow_phase1b_policy.parquet`` (per-intent, incl.
    variant) under ``--analysis-dir`` (default /root/openclaw/analysis).

Pre-registration doctrine (spec §2, locked): a NULL here = KILL at minute scale,
the tick thesis is INCONCLUSIVE-and-discouraged (NOT disproven). Test A is the
ONLY gate; Test B is supportive and feeds only the KILL conjunct. Momentum-sign
significance is GO-with-rule-inverted (§3), a first-class finding, never a kill.

Determinism: no randomness; every dict/set iteration is sorted so the report
text and the parquet row order are stable run-to-run.

Dependencies: pandas + numpy only (Spearman is rank-then-Pearson; no scipy).
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import re
import sys

import numpy as np
import pandas as pd

from src.research.bflow import flow_features as ff
from src.research.bflow import flow_policy as fp
from src.research.bflow import oracle
from src.research.bflow import predictability as pr

_DEFAULT_CACHE_DIR = "/root/openclaw/data/cache/min_bars"
_DEFAULT_ORDERS_PATH = "/root/openclaw/analysis/bflow_phase1_oracle_orders.parquet"
_DEFAULT_ANALYSIS_DIR = "/root/openclaw/analysis"

# Expected Phase-1 primary-grain included-intent count on real data (spec §2/§4).
EXPECTED_TESTB_INTENTS = 3867

# Cache file name -> session date.
_CACHE_FILE_RE = re.compile(r"^min_bars_(\d{4}-\d{2}-\d{2})\.parquet$")

_FEATURES = ("ofi_5", "ofi_15", "vwap_disp_30")
_HORIZONS = ("ret_fwd_5", "ret_fwd_15", "ret_fwd_30", "ret_fwd_60")
_VARIANTS = ("reversion", "momentum")

# §5 verdict thresholds (pre-committed — do NOT tune).
PRIMARY_T_GO = 3.0          # |t| >= 3 on the primary target = a survivor
SECONDARY_T_GO = 2.0        # |t| >= 2 at a secondary horizon = coherent
KILL_T = 2.0                # all primary |t| < 2 (KILL conjunct 1)
MIN_SURVIVORS = 2           # >=2 of 3 features survive
MIN_COHERENT_HORIZONS = 2   # >=2 of the 4 secondary horizons agree

# Entry-minute histogram buckets (spec §4 reporting; mirrors run_phase1).
_HIST_BUCKETS = (
    ("0-29", 0, 29),
    ("30-89", 30, 89),
    ("90-329", 90, 329),
    ("330-374", 330, 374),
    ("375-389", 375, 389),
)

# The pre-registration framing quote (load-bearing — spec §2/§6; asserted by the
# smoke test and by reviewers).
_PREREG_QUOTE = (
    "Pre-registration framing: a null here = KILL at minute scale, the tick "
    "thesis is inconclusive-and-discouraged (not disproven). This verdict was "
    "committed before any eval run (spec §2/§3/§5)."
)


# --------------------------------------------------------------------------
# NaN-safe magnitude tests (the codebase `not (x > 0)` idiom)
# --------------------------------------------------------------------------
def _abs_ge(t, thresh):
    """True iff |t| >= thresh, NaN-safe. ``abs(NaN) >= thresh`` is False, so a
    NaN/None t is correctly NOT significant (never raises, never True on NaN)."""
    x = oracle._f(t)
    return bool(abs(x) >= thresh)


def _sign(t):
    """+1 / -1 / 0 for a finite t; 0 for NaN/None (a non-finite t carries no
    sign and never participates in a sign-consistency vote)."""
    x = oracle._f(t)
    if not np.isfinite(x):
        return 0
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# --------------------------------------------------------------------------
# §5 verdict — PURE, unit-testable
# --------------------------------------------------------------------------
def _primary_survivors(primary_summary):
    """Features with |t| >= PRIMARY_T_GO on the primary target (ret_to_dump),
    in fixed _FEATURES order (deterministic)."""
    return [f for f in _FEATURES
            if f in primary_summary and _abs_ge(primary_summary[f], PRIMARY_T_GO)]


def _shared_sign(primary_summary):
    """The single sign shared by ALL primary survivors, or None if there are no
    survivors OR the survivors disagree in sign. (Sub-threshold features and
    NaN t-values never enter this vote — significance is pinned to the |t|>=3
    set, exactly per §5.)"""
    survivors = _primary_survivors(primary_summary)
    if not survivors:
        return None
    signs = {_sign(primary_summary[f]) for f in survivors}
    if len(signs) != 1:
        return None
    (s,) = signs
    return s if s != 0 else None


def sign_regime(primary_summary):
    """'reversion' if the consistent primary survivor sign is negative,
    'momentum' if positive, None if no consistent survivor sign (spec §3).

    reversion = the operator's rule architecture as designed; momentum = GO with
    the rule inverted (a first-class finding, NOT a kill)."""
    s = _shared_sign(primary_summary)
    if s is None:
        return None
    return "reversion" if s < 0 else "momentum"


def _coherent(secondary_summary, survivors, shared_sign):
    """Coherence requirement (§5): for >=1 of the PRIMARY SURVIVORS, that
    feature shows |t| >= SECONDARY_T_GO with sign == shared_sign in >=
    MIN_COHERENT_HORIZONS of its 4 secondary horizons.

    Per-feature over its OWN 4 secondaries (never pooled across features); a weak
    primary feature cannot lend coherence to a survivor."""
    for f in survivors:
        horizons = secondary_summary.get(f, {})
        agree = 0
        for h in _HORIZONS:
            t = horizons.get(h)
            if _abs_ge(t, SECONDARY_T_GO) and _sign(t) == shared_sign:
                agree += 1
        if agree >= MIN_COHERENT_HORIZONS:
            return True
    return False


def verdict(primary_summary, secondary_summary, testb_summary):
    """The PRE-COMMITTED §5 verdict. PURE — depends only on the three summary
    dicts so it is trivially unit-testable.

    Parameters
    ----------
    primary_summary : {feature: t} — across-session clustered t of each feature
        vs the PRIMARY target ret_to_dump.
    secondary_summary : {feature: {horizon: t}} — same t vs each of the 4 forward
        horizons (ret_fwd_5/15/30/60).
    testb_summary : {"reversion_across_session_mean": float} — the Test-B
        reversion-variant across-session mean delta_bps (the KILL conjunct;
        momentum NEVER feeds the verdict).

    Returns ``(result, reasons)`` with ``result`` in {'GO','KILL','WEAK'} and
    ``reasons`` a list[str] documenting the decision (incl. the sign_regime).

    GO  = (>=2 of 3 features |t|>=3 on ret_to_dump, all survivors sharing one
          sign) AND (that same sign with |t|>=2 in >=2 of the 4 secondary
          horizons for >=1 of those survivors).  Reversion sign => operator's
          architecture; momentum sign => GO with the rule inverted.
    KILL = all primary |t| < 2 AND reversion-variant across-session mean <= 0.
    WEAK = otherwise (operator call): lone-max-t, sign-inconsistent survivors,
          coherence-fail, or a KILL-near-miss (all-<2 but testb mean > 0).

    GO and KILL are mutually exclusive on the primary leg (GO needs >=2 |t|>=3;
    KILL needs all |t|<2), so the GO->KILL->WEAK order is safe.
    """
    reasons = []
    survivors = _primary_survivors(primary_summary)
    shared = _shared_sign(primary_summary)
    regime = sign_regime(primary_summary)

    reasons.append(
        "primary survivors (|t|>=3 on ret_to_dump): "
        + (", ".join(survivors) if survivors else "none"))

    # ---- GO ----
    if len(survivors) >= MIN_SURVIVORS and shared is not None:
        coherent = _coherent(secondary_summary, survivors, shared)
        if coherent:
            reasons.append(
                f"sign_regime={regime} (shared primary sign "
                f"{'negative' if shared < 0 else 'positive'})")
            reasons.append(
                "coherence met: >=1 survivor has |t|>=2 with the shared sign in "
                ">=2 of 4 secondary horizons")
            if regime == "momentum":
                reasons.append(
                    "momentum sign => GO with the rule inverted (buy into "
                    "buy-flow); first-class finding, NOT a kill (prereg §3)")
            return "GO", reasons
        reasons.append(
            "coherence NOT met (lone max-t / no >=2 coherent secondary "
            "horizons) -> not GO")
    elif len(survivors) >= MIN_SURVIVORS and shared is None:
        reasons.append("primary survivors disagree in sign -> not GO")
    else:
        reasons.append(
            f"only {len(survivors)} survivor(s) (need >={MIN_SURVIVORS}) -> "
            "not GO")

    # ---- KILL ----
    all_below_kill = not any(
        _abs_ge(primary_summary.get(f), KILL_T) for f in _FEATURES)
    rev_mean = oracle._f(testb_summary.get("reversion_across_session_mean"))
    testb_nonpositive = not (rev_mean > 0)   # NaN-safe: NaN -> True (no edge)
    if all_below_kill and testb_nonpositive:
        reasons.append(
            "KILL: all primary |t| < 2 AND reversion-variant across-session "
            f"mean delta = {rev_mean:.3f} <= 0 (minute scale; tick scale "
            "inconclusive/discouraged)")
        return "KILL", reasons

    if all_below_kill:
        reasons.append(
            f"all primary |t| < 2 but Test-B reversion mean {rev_mean:.3f} > 0 "
            "-> KILL near-miss -> WEAK")
    else:
        reasons.append(
            "not KILL: >=1 primary |t| >= 2 (some predictive structure, but "
            "below the GO bar) -> WEAK")
    reasons.append("WEAK -> operator call")
    return "WEAK", reasons


# --------------------------------------------------------------------------
# cache-only session discovery + loading
# --------------------------------------------------------------------------
def enumerate_cache_sessions(cache_dir):
    """Sorted list of session dates (YYYY-MM-DD) that have a
    ``min_bars_<session>.parquet`` in ``cache_dir``. A missing directory -> []."""
    if not os.path.isdir(cache_dir):
        return []
    out = []
    for name in os.listdir(cache_dir):
        m = _CACHE_FILE_RE.match(name)
        if m:
            out.append(m.group(1))
    return sorted(out)


def load_session_frame(cache_dir, session):
    """Read the per-session cache parquet -> DataFrame (ticker,minute,o,h,l,c,
    v,vw), dropping the minute=-1 empty-sentinel rows (minbar_cache convention).
    Returns None if the file is absent (CACHE-ONLY: never fetched)."""
    path = os.path.join(cache_dir, f"min_bars_{session}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if "minute" in df.columns:
        df = df[pd.to_numeric(df["minute"], errors="coerce") >= 0]
    return df


def _ticker_frames(session_df):
    """Split a session DataFrame into {ticker: per-ticker DataFrame} (minute +
    OHLCV columns), sorted by ticker for deterministic pooling."""
    out = {}
    for tk in sorted(t for t in session_df["ticker"].dropna().unique()):
        sub = session_df[session_df["ticker"] == tk].copy()
        out[str(tk)] = sub
    return out


# --------------------------------------------------------------------------
# Test A — per-session pooled ICs over the whole cache
# --------------------------------------------------------------------------
def run_test_a(cache_dir, sessions, progress=True):
    """For each cached session: split into per-ticker frames, compute each
    ticker's dump benchmark via ``oracle.dump_benchmark`` (it iterates dicts ->
    feed ``df.to_dict('records')``), and run ``predictability.session_ics``.

    Returns ``(per_session_ics, missing_sessions, session_obs)`` where
    per_session_ics is a list of (session, {cell: ic}); missing_sessions are
    sessions with no cache file; session_obs maps session -> # tickers passing
    the 60-valid-bar floor (data-quality)."""
    per_session_ics = []
    missing = []
    session_obs = {}
    for session in sessions:
        sdf = load_session_frame(cache_dir, session)
        if sdf is None or not len(sdf):
            missing.append(session)
            if progress:
                print(f"[bflow-p1b][A] {session}: MISSING cache file -> skipped",
                      flush=True)
            continue
        frames = _ticker_frames(sdf)
        dump = {}
        n_qualifying = 0
        for tk, tdf in frames.items():
            dump[tk] = oracle.dump_benchmark(tdf.to_dict("records"))
            if pr._valid_bar_count(tdf) >= pr.MIN_VALID_BARS:
                n_qualifying += 1
        ics = pr.session_ics(frames, dump)
        per_session_ics.append((session, ics))
        session_obs[session] = n_qualifying
        if progress:
            print(f"[bflow-p1b][A] {session}: {len(frames)} tickers, "
                  f"{n_qualifying} pass 60-bar floor", flush=True)
    return per_session_ics, missing, session_obs


def _summary_to_dicts(summary):
    """Convert the predictability ``summarize`` DataFrame (index = cell names
    feature|target) into the verdict's two summary dicts:
      primary_summary    = {feature: t} for target ret_to_dump,
      secondary_summary  = {feature: {horizon: t}} for the 4 forward horizons.
    """
    primary = {}
    secondary = {f: {} for f in _FEATURES}
    for f in _FEATURES:
        primary[f] = float(summary.loc[pr.cell_name(f, "ret_to_dump"), "t"])
        for h in _HORIZONS:
            secondary[f][h] = float(summary.loc[pr.cell_name(f, h), "t"])
    return primary, secondary


# --------------------------------------------------------------------------
# Test B — causal proxy policy over the Phase-1 primary-grain intents
# --------------------------------------------------------------------------
def load_testb_intents(orders_path):
    """Load the Phase-1 primary-grain included intents from the oracle-orders
    parquet: grain=='primary' AND exclude_reason is null. Returns a list of
    dicts {worked_session, ticker, direction, p_eod_dump}.

    The count is asserted-and-logged against EXPECTED_TESTB_INTENTS on real data
    by the caller (``run_test_b``); this loader does not hard-fail (tests inject
    a tiny synthetic parquet)."""
    df = pd.read_parquet(orders_path)
    mask = (df["grain"] == "primary") & (df["exclude_reason"].isnull())
    sub = df[mask]
    intents = []
    for _, r in sub.iterrows():
        intents.append({
            "worked_session": str(r["worked_session"]),
            "ticker": str(r["ticker"]),
            "direction": str(r["direction"]),
            "p_eod_dump": (None if pd.isna(r["p_eod_dump"])
                           else float(r["p_eod_dump"])),
        })
    # deterministic order
    intents.sort(key=lambda x: (x["worked_session"], x["ticker"],
                                x["direction"]))
    return intents


def run_test_b(cache_dir, intents, progress=True):
    """Run ``flow_policy.simulate_intent`` for BOTH variants over the Test-B
    intents, joining each to its (worked_session, ticker) cached bars and
    reusing the parquet's ``p_eod_dump``.

    Returns ``(policy_rows, missing_sessions)``. policy_rows is one dict per
    (intent, variant) carrying the intent fields, the variant, and the
    simulate_intent output (triggered/entry_minute/entry_price/used_fallback/
    delta_bps/arm_count). An intent whose ticker has no cached bars becomes a row
    with ``no_bars=True`` (counted in data-quality, never silently dropped).

    Bars are loaded ONCE per distinct session (batch realism); the cache is read
    only (CACHE-ONLY — never fetched)."""
    by_session = {}
    for it in intents:
        by_session.setdefault(it["worked_session"], []).append(it)

    policy_rows = []
    missing = []
    for session in sorted(by_session):
        sdf = load_session_frame(cache_dir, session)
        if sdf is None or not len(sdf):
            missing.append(session)
            for it in by_session[session]:
                for variant in _VARIANTS:
                    policy_rows.append(_no_bars_policy_row(it, variant))
            if progress:
                print(f"[bflow-p1b][B] {session}: MISSING cache file -> "
                      f"{len(by_session[session])} intents no_bars", flush=True)
            continue
        frames = _ticker_frames(sdf)
        n_done = 0
        for it in sorted(by_session[session],
                         key=lambda x: (x["ticker"], x["direction"])):
            tdf = frames.get(it["ticker"])
            if tdf is None or not len(tdf):
                for variant in _VARIANTS:
                    policy_rows.append(_no_bars_policy_row(it, variant))
                continue
            for variant in _VARIANTS:
                res = fp.simulate_intent(
                    tdf, it["direction"], it.get("p_eod_dump"), variant=variant)
                policy_rows.append({**_intent_fields(it), "variant": variant,
                                    "no_bars": False, **res})
            n_done += 1
        if progress:
            print(f"[bflow-p1b][B] {session}: simulated {n_done} intents "
                  f"x {len(_VARIANTS)} variants", flush=True)
    return policy_rows, missing


def _intent_fields(it):
    return {
        "worked_session": it["worked_session"],
        "ticker": it["ticker"],
        "direction": it["direction"],
        "p_eod_dump": it.get("p_eod_dump"),
    }


def _no_bars_policy_row(it, variant):
    return {
        **_intent_fields(it), "variant": variant, "no_bars": True,
        "triggered": False, "entry_minute": None, "entry_price": None,
        "used_fallback": False, "delta_bps": float("nan"), "arm_count": 0,
    }


# --------------------------------------------------------------------------
# Test B economics — per-session mean delta, across-session mean + clustered t
# --------------------------------------------------------------------------
def testb_economics(policy_rows, variant):
    """Per-session mean delta_bps for ``variant``, then across-session mean and
    clustered t = mean / (sd / sqrt(n_sessions)) — SAME convention as
    ``predictability.summarize`` (sample sd, ddof=1; n<2 -> sd/t NaN).

    Also: trigger rate, fallback rate, and (reversion only, by the caller) the
    entry-minute histogram. Excludes ``no_bars`` rows from the economics (they
    carry NaN delta and represent an unpriceable intent). Returns a dict."""
    rows = [r for r in policy_rows
            if r.get("variant") == variant and not r.get("no_bars")]
    n_intents = len(rows)
    n_triggered = sum(1 for r in rows if r.get("triggered"))
    n_fallback = sum(1 for r in rows if r.get("used_fallback"))

    by_session = {}
    for r in rows:
        d = oracle._f(r.get("delta_bps"))
        if np.isfinite(d):
            by_session.setdefault(r["worked_session"], []).append(d)

    session_means = []
    for s in sorted(by_session):
        vals = by_session[s]
        if vals:
            session_means.append(float(np.mean(vals)))

    n_sessions = len(session_means)
    if n_sessions > 0:
        mean = float(np.mean(session_means))
    else:
        mean = float("nan")
    if n_sessions > 1:
        sd = float(np.std(session_means, ddof=1))
        t = mean / (sd / math.sqrt(n_sessions)) if sd > 0 else float("nan")
    else:
        sd = float("nan")
        t = float("nan")

    return {
        "variant": variant,
        "n_intents": n_intents,
        "n_triggered": n_triggered,
        "n_fallback": n_fallback,
        "trigger_rate": (n_triggered / n_intents) if n_intents else float("nan"),
        "fallback_rate": (n_fallback / n_intents) if n_intents else float("nan"),
        "n_sessions": n_sessions,
        "across_session_mean_delta_bps": mean,
        "sd_session_delta_bps": sd,
        "clustered_t": t,
    }


def entry_minute_histogram(policy_rows, variant="reversion"):
    """Bucketed entry-minute histogram for ``variant`` (reversion by default,
    spec §4). Only triggered (non-fallback) entries with an entry_minute."""
    hist = {label: 0 for label, _, _ in _HIST_BUCKETS}
    for r in policy_rows:
        if r.get("variant") != variant or r.get("no_bars"):
            continue
        m = r.get("entry_minute")
        if m is None:
            continue
        for label, lo, hi in _HIST_BUCKETS:
            if lo <= m <= hi:
                hist[label] += 1
                break
    return hist


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _fmt(x, suffix=""):
    v = oracle._f(x)
    if not np.isfinite(v):
        return "n/a"
    return f"{v:+.3f}{suffix}"


def _sign_vs_prereg(t):
    """The pre-registered sign is reversion => IC/t < 0 (spec §3). Report each
    feature's actual sign vs that expectation. NaN -> 'n/a'."""
    s = _sign(t)
    if oracle._f(t) != oracle._f(t):   # NaN
        return "n/a"
    if s < 0:
        return "reversion (matches prereg)"
    if s > 0:
        return "momentum (opposite — GO-inverted, not a kill)"
    return "flat"


def format_report(result, reasons, primary_summary, secondary_summary,
                  summary, testb, dataq):
    """Render the spec §6 markdown report. Load-bearing strings (asserted by the
    tests / reviewers): 'VERDICT', the pre-registration framing quote, the
    PRIMARY grid table, the SECONDARY grid, the Test-B reversion headline +
    momentum diagnostic (labeled non-gating), and the data-quality section."""
    rev = testb["reversion"]
    mom = testb["momentum"]
    L = []
    L.append("# SP-6 B-flow Phase 1b — Order-Flow Predictability Kill-Test Report")
    L.append("")
    L.append(f"**VERDICT: {result}**")
    regime = sign_regime(primary_summary)
    regime_label = regime if regime else "n/a (no consistent survivor sign)"
    L.append(f"- sign_regime: {regime_label}")
    for r in reasons:
        L.append(f"- {r}")
    L.append("")

    # pre-registration framing paragraph (spec §2/§6 — load-bearing quote)
    L.append("## Pre-registration framing")
    L.append(_PREREG_QUOTE)
    L.append("Test A is the ONLY gate (the clustered across-session t on the "
             "minute-flow features vs forward returns). Test B is supportive, "
             "NOT gating: it feeds only the KILL conjunct (reversion-variant "
             "across-session mean delta <= 0).")
    L.append("")

    # PRIMARY grid table
    L.append("## PRIMARY grid — feature vs ret_to_dump (GATING)")
    L.append("| feature | mean IC | t | n_sessions | sign-vs-prereg |")
    L.append("|---|---|---|---|---|")
    for f in _FEATURES:
        cell = pr.cell_name(f, "ret_to_dump")
        mean_ic = summary.loc[cell, "mean_ic"]
        t = summary.loc[cell, "t"]
        n = summary.loc[cell, "n_sessions"]
        L.append(f"| {f} | {_fmt(mean_ic)} | {_fmt(t)} | "
                 f"{int(n) if pd.notna(n) else 0} | {_sign_vs_prereg(t)} |")
    L.append("")

    # SECONDARY grid (4 horizons x 3 features)
    L.append("## SECONDARY grid — t at 4 forward horizons x 3 features")
    L.append("| feature | " + " | ".join(_HORIZONS) + " |")
    L.append("|---|" + "|".join(["---"] * len(_HORIZONS)) + "|")
    for f in _FEATURES:
        cells = [_fmt(secondary_summary[f][h]) for h in _HORIZONS]
        L.append(f"| {f} | " + " | ".join(cells) + " |")
    L.append("")

    # Test-B economics
    L.append("## Test-B economics (causal proxy policy; SUPPORTIVE, NOT GATING)")
    L.append("### Reversion variant (the pre-registered rule — HEADLINE)")
    L.append(f"- across-session mean delta vs P_eod_dump: "
             f"{_fmt(rev['across_session_mean_delta_bps'], 'bps')} "
             f"(clustered t = {_fmt(rev['clustered_t'])}, "
             f"n_sessions = {rev['n_sessions']})")
    L.append(f"- trigger rate: {_rate(rev['trigger_rate'])}  |  "
             f"fallback rate: {_rate(rev['fallback_rate'])}  "
             f"(n_intents = {rev['n_intents']})")
    L.append("- entry-minute histogram (triggered entries, minutes from 09:30):")
    for label, _, _ in _HIST_BUCKETS:
        L.append(f"  - {label}: {dataq['entry_hist'][label]}")
    L.append("")
    L.append("### Momentum variant (DIAGNOSTIC ONLY — NON-GATING)")
    L.append("Enter WITH the burst (arm OFI sign flipped). Pre-registered as "
             "diagnostic, never gating; reported for completeness only.")
    L.append(f"- across-session mean delta vs P_eod_dump: "
             f"{_fmt(mom['across_session_mean_delta_bps'], 'bps')} "
             f"(clustered t = {_fmt(mom['clustered_t'])}, "
             f"n_sessions = {mom['n_sessions']})")
    L.append(f"- trigger rate: {_rate(mom['trigger_rate'])}  |  "
             f"fallback rate: {_rate(mom['fallback_rate'])}")
    L.append("")

    # data-quality
    L.append("## Data-quality")
    L.append(f"- Test-A cache sessions: {dataq['n_sessions_a']} present, "
             f"{len(dataq['missing_a'])} missing"
             + (" (" + ", ".join(dataq['missing_a']) + ")"
                if dataq['missing_a'] else ""))
    L.append(f"- Test-A per-session tickers passing the 60-valid-bar floor "
             f"(median): {dataq['median_obs_a']}")
    match_label = ("MATCH" if dataq["testb_count_match"]
                   else "differs — synthetic or partial cache")
    L.append(f"- Test-B intents: {dataq['n_testb_intents']} included primary "
             f"(grain=='primary' AND exclude_reason is null); expected "
             f"{EXPECTED_TESTB_INTENTS} on real data ({match_label}).")
    L.append(f"- Test-B intents with no cached bars (counted, not dropped): "
             f"{dataq['n_testb_no_bars']}")
    L.append(f"- Test-B missing cache sessions: {len(dataq['missing_b'])}"
             + (" (" + ", ".join(dataq['missing_b']) + ")"
                if dataq['missing_b'] else ""))
    L.append("- per-session-cell observation floor: NONE (spec §3 fixes no "
             "per-cell minimum; every finite (feature,target) pair in the "
             "session counts toward its cell's IC). A cell is NaN only when its "
             "pool is empty or its ranked feature/target is constant "
             "(intrinsically-undefined correlation).")
    L.append(f"- 60-valid-bar floor: a (ticker, session) participates in Test A "
             "only with >= 60 valid bars (vw>0, v>0, h>=l); the Phase-1 floor "
             "reused verbatim.")
    L.append(f"- Test-B fallback rates — reversion "
             f"{_rate(rev['fallback_rate'])}, momentum "
             f"{_rate(mom['fallback_rate'])} (a fallback intent entered AT the "
             "dump -> delta 0bps, honestly diluting the mean toward 0).")
    L.append("")
    return "\n".join(L)


def _rate(x):
    v = oracle._f(x)
    if not np.isfinite(v):
        return "n/a"
    return f"{v:.1%}"


# --------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------
_POLICY_COLUMNS = [
    "worked_session", "ticker", "direction", "variant", "p_eod_dump",
    "no_bars", "triggered", "entry_minute", "entry_price", "used_fallback",
    "delta_bps", "arm_count",
]


def write_artifacts(report_md, grid, policy_rows, analysis_dir):
    """Write the markdown report + the two parquets. Returns
    (report_path, ic_grid_path, policy_path). Creates the directory."""
    os.makedirs(analysis_dir, exist_ok=True)
    report_path = os.path.join(analysis_dir, "bflow_phase1b_report.md")
    ic_grid_path = os.path.join(analysis_dir, "bflow_phase1b_ic_grid.parquet")
    policy_path = os.path.join(analysis_dir, "bflow_phase1b_policy.parquet")

    with open(report_path, "w") as fh:
        fh.write(report_md)

    # ic_grid: sessions x cells (a copy with the session index materialised as a
    # column so the parquet round-trips the labels).
    grid_out = grid.reset_index()
    grid_out.to_parquet(ic_grid_path, index=False)

    policy_df = pd.DataFrame(
        [{c: r.get(c) for c in _POLICY_COLUMNS} for r in policy_rows],
        columns=_POLICY_COLUMNS)
    policy_df.to_parquet(policy_path, index=False)

    return report_path, ic_grid_path, policy_path


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def _parse_args(argv):
    p = argparse.ArgumentParser(prog="run_phase1b")
    p.add_argument("--cache-dir", default=_DEFAULT_CACHE_DIR,
                   help="minute-bar cache dir (CACHE-ONLY; never fetched)")
    p.add_argument("--orders-path", default=_DEFAULT_ORDERS_PATH,
                   help="Phase-1 oracle-orders parquet (Test-B universe)")
    p.add_argument("--analysis-dir", default=_DEFAULT_ANALYSIS_DIR,
                   help="output dir for the report + parquets")
    p.add_argument("--limit", type=int, default=None,
                   help="cap to the first N cache sessions (debug)")
    return p.parse_args(argv)


def main(argv=None):
    """CLI entry. CACHE-ONLY (never fetches); prints per-session progress to
    stdout (flush=True for unbuffered-friendly logs). Returns a process exit
    code (0 = success)."""
    args = _parse_args([] if argv is None else argv)

    sessions = enumerate_cache_sessions(args.cache_dir)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    print(f"[bflow-p1b] cache dir {args.cache_dir}: {len(sessions)} sessions "
          f"(limit={args.limit})", flush=True)

    # ---- Test A (GATING) ----
    per_session_ics, missing_a, session_obs = run_test_a(args.cache_dir, sessions)
    grid = pr.ic_grid(per_session_ics)
    summary = pr.summarize(grid)
    primary_summary, secondary_summary = _summary_to_dicts(summary)

    # ---- Test B (SUPPORTIVE) ----
    intents = load_testb_intents(args.orders_path)
    n_testb = len(intents)
    count_match = (n_testb == EXPECTED_TESTB_INTENTS)
    if count_match:
        print(f"[bflow-p1b][B] Test-B intents: {n_testb} (MATCH expected "
              f"{EXPECTED_TESTB_INTENTS})", flush=True)
    else:
        print(f"[bflow-p1b][B] Test-B intents: {n_testb} (expected "
              f"{EXPECTED_TESTB_INTENTS} on real data — synthetic/partial "
              "cache, not hard-failing)", flush=True)
    # Under --limit (debug) restrict Test-B to the capped session window so a
    # quick run stays self-consistent. On the FULL path do NOT pre-filter: an
    # intent whose worked_session has no cache file must fall through to
    # run_test_b's missing-session branch and be COUNTED as no_bars (spec §2:
    # "if a needed session file is absent, count it in data-quality and
    # continue"), never silently dropped.
    if args.limit is not None:
        cache_session_set = set(sessions)
        intents = [it for it in intents
                   if it["worked_session"] in cache_session_set]

    policy_rows, missing_b = run_test_b(args.cache_dir, intents)
    testb = {v: testb_economics(policy_rows, v) for v in _VARIANTS}
    entry_hist = entry_minute_histogram(policy_rows, variant="reversion")

    # ---- verdict (§5) ----
    testb_summary = {
        "reversion_across_session_mean":
            testb["reversion"]["across_session_mean_delta_bps"],
    }
    result, reasons = verdict(primary_summary, secondary_summary, testb_summary)

    # ---- data-quality bundle ----
    obs_vals = sorted(session_obs.values())
    median_obs = (oracle.per_session_median(obs_vals)
                  if obs_vals else None)
    n_testb_no_bars = sum(1 for r in policy_rows
                          if r.get("no_bars") and r.get("variant") == "reversion")
    dataq = {
        "n_sessions_a": len(per_session_ics),
        "missing_a": missing_a,
        "median_obs_a": median_obs,
        "n_testb_intents": n_testb,
        "testb_count_match": count_match,
        "n_testb_no_bars": n_testb_no_bars,
        "missing_b": missing_b,
        "entry_hist": entry_hist,
    }

    report_md = format_report(result, reasons, primary_summary,
                              secondary_summary, summary, testb, dataq)

    report_path, ic_grid_path, policy_path = write_artifacts(
        report_md, grid, policy_rows, args.analysis_dir)
    print(report_md)
    print(f"[bflow-p1b] VERDICT: {result}", flush=True)
    print(f"[bflow-p1b] wrote {report_path}", flush=True)
    print(f"[bflow-p1b] wrote {ic_grid_path}", flush=True)
    print(f"[bflow-p1b] wrote {policy_path}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
