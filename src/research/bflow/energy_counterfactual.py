"""SP-6 B-flow Phase 1c — ENERGY symmetric two-leg execution counterfactual (M3).

T3 of the Phase-1c ENERGY deep-dive. Binding spec:
``analysis/bflow_phase1c_energy_grid_preenumeration.md`` §2 method M3 (committed
pre-results; the grid/method set may not be expanded or shrunk). M3 is the ONLY
evaluation method that touches a SYNTHETIC order book and bps; the IC grid (M1)
and the leave-one-week-out CV (M2) are the upstream T2 objects
(``energy_grid`` / ``energy_features``) and stay on rank-correlation. M4 (the
dispositive real-intent OOS gate, sessions ≥ 2026-06-08) is explicitly NOT built
in this pass (grid-scope guard).

M3 in one paragraph (spec §2):
  For EVERY cached (ticker, session ≤ insample_end) pair, simulate BOTH a BUY leg
  and a SELL leg on a SYNTHETIC book (NOT our real intents), each timed by the
  FROZEN energy signal = E1 W=15 GLOBAL-CAUSAL-β residual r. The trigger uses the
  SESSION-CENTERED r: subtract the RUNNING (trailing-only, causal) session mean
  and divide by the RUNNING session sd, then apply the z-threshold with the
  reversion sign — BUY enters when centered r ≤ −z (undershoot ⇒ upward pressure
  ⇒ buy into it), SELL enters when centered r ≥ +z (overshoot ⇒ downward pressure
  ⇒ sell into it). First crossing only; one entry per leg. Fill at vw_{t+1};
  fallback = dump at minute FALLBACK_MINUTE (384) if never triggered; the Phase-1
  differential cost applied EXACTLY as Test-B. Frozen z ∈ {0.5, 1.0, 1.5}, ALL
  reported. Scoring: per-leg per-session MEAN delta-bps (the SESSION is the
  cluster), across-session clustered t per leg. PASS = BOTH legs INDIVIDUALLY
  clustered-positive; the buy+sell SUM is the drift-control DIAGNOSTIC, never the
  bar. Report trigger rate + fallback rate per z.

WHY both-legs + session-centering kill the Test-B confound (spec §2):
  A session-level price drift fires one leg far more than the other (drift up ⇒
  the BUY leg's undershoot crossings cluster, the SELL leg's starve) and lifts
  the favoured leg's mean while sinking the other's. The both-legs-positive
  criterion can NEVER be satisfied by drift (it helps one, hurts the other); the
  session-centering removes the slow drift component from the trigger itself so a
  leg cannot be fired asymmetrically by trend. The SUM is reported only as the
  drift-control diagnostic — a non-zero sum flags residual drift, it is never the
  pass bar.

DOCUMENTED CHOICES (spec silent → harness convention, per the standing
constraint; flagged here, NOT scanned):

  (1) RUNNING z is t-INCLUSIVE (the trailing window is r_0..r_t, including t).
      Rationale: r_t is known at the decision time t (it is built from bars ≤ t),
      so excluding it would discard information available at decision time; the
      recommended reading in the grounding map. sd is the SAMPLE sd (ddof=1),
      matching the clustered-t convention used everywhere in this harness; z is
      NaN until ≥ 2 finite trailing r (sd undefined on < 2) and NaN where the
      trailing sd is 0 (a degenerate, constant prefix). NaN z never crosses any
      threshold (the ``not (x op y)`` idiom), so a degenerate prefix simply
      delays the first possible entry — it never fires spuriously.

  (2) SCAN starts at SCAN_START_MINUTE (30) — reused verbatim from
      ``flow_policy.SCAN_START_MINUTE`` ("reuse flow_policy conventions", spec
      §2). z is computed over the full causal prefix (so σ has warmed up by
      minute 30), but only crossings at minute ≥ 30 may fire.

  (3) The z-trigger REPLACES BOTH ``flow_policy._arm_condition`` (the OFI burst
      arm) AND ``flow_policy._trigger_condition`` (the price-confirm) — there is
      NO price confirm in M3; the energy z IS the whole trigger. Everything
      DOWNSTREAM of the decision is reused VERBATIM by import from
      ``flow_policy`` / ``oracle``: the vw_{t+1} causal fill, the lapse-on-invalid
      -t+1 rule, the FALLBACK_MINUTE=384 dump fallback (delta_bps = 0 by
      construction), and ``flow_policy._delta_bps`` (the Phase-1 differential
      spread cost; the +2bps impact cancels in the differential). buy ⇒ LONG,
      sell ⇒ SHORT in the oracle gross/cost math. flow_policy is NOT modified
      (that would touch the Phase-1b Test-B tests).

  (4) The FROZEN signal is byte-identical to ``energy_grid``'s E1 W=15
      GLOBAL-CAUSAL β (NEVER the per-session lookahead — an execution sim must be
      causal). We obtain it by the grid's OWN fit path (``ef.fit_global_linear``
      over ``eg._e1_bundles``) so the two cannot diverge. r is then formed
      PER TICKER along the minute axis (``ef.apply_linear_residual`` on this
      ticker's own disp_15 / ofi_15) — NEVER pooled across tickers: the running z
      is a temporal walk and pooling would carry one ticker's running stats into
      the next (the exact E2-propagator boundary hazard ``energy_grid``
      documents).

  (5) The INTERCEPT reading is IMMATERIAL to M3 (unlike E4/E5): adding the OLS
      constant a to r shifts the running mean by a and leaves the running sd
      untouched, so the session-centered z = (r − μ)/σ is unchanged. M3 therefore
      uses ONLY the canonical with-intercept residual and emits NO no-intercept
      diagnostic axis. (``test_running_z_shift_invariant`` pins this.)

  (6) The qualifying universe is the SAME 60-valid-bar floor and the SAME
      in-sample cache enumeration as the grid (``energy_grid`` /
      ``exploratory_phase1c.enumerate_sessions``), so M3 scores the identical
      34-session in-sample set. CACHE-ONLY: the driver never fetches.

PURITY: ``running_z`` / ``ticker_signal`` / ``simulate_leg`` /
``summarize_counterfactual`` / ``build_counterfactual`` are pure (series / frames
in, dict / frame out). Cache loading + the parquet write live in a thin
``main()`` mirroring ``energy_grid.run`` / ``write_grid``.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from src.research.bflow import energy_features as ef
from src.research.bflow import energy_grid as eg
from src.research.bflow import exploratory_phase1c as p1c
from src.research.bflow import flow_features as ff
from src.research.bflow import flow_policy as fp
from src.research.bflow import oracle
from src.research.bflow import predictability as pr

# ---- frozen method parameters (spec §2; do NOT tune) ------------------------
Z_GRID = (0.5, 1.0, 1.5)            # frozen z-threshold grid, all reported
FROZEN_WINDOW = 15                  # E1 W=15 global-causal-β residual (frozen)
LEGS = ("buy", "sell")             # buy ⇒ LONG, sell ⇒ SHORT
SCAN_START_MINUTE = fp.SCAN_START_MINUTE   # 30 (reused verbatim)
FALLBACK_MINUTE = fp.FALLBACK_MINUTE       # 384 (reused verbatim)

INSAMPLE_END = eg.INSAMPLE_END             # "2026-06-02"
DEFAULT_CACHE_DIR = eg.DEFAULT_CACHE_DIR
DEFAULT_ANALYSIS_DIR = eg.DEFAULT_ANALYSIS_DIR
MIN_VALID_BARS = pr.MIN_VALID_BARS

_DIRECTION_FOR_LEG = {"buy": "LONG", "sell": "SHORT"}

# Tidy output columns (deterministic; spec §2 Output contract).
OUTPUT_COLUMNS = ["z", "leg", "mean_bps", "t", "n_sessions",
                  "trigger_rate", "fallback_rate", "extra"]


# ==========================================================================
# 1. running causal session-centered z  (pure, standalone)
# ==========================================================================
def running_z(r):
    """SESSION-CENTERED z of an energy-signal series ``r`` along the minute axis:
    z_t = (r_t − running_mean_t) / running_sd_t with TRAILING-ONLY, t-INCLUSIVE
    running stats (the window is r_0..r_t over the FINITE entries seen so far).

    Strictly causal: z_t depends only on r_0..r_t — altering any minute > t leaves
    z_t byte-identical (``test_running_z_causal_future_minute_does_not_change_past``).
    sd is the SAMPLE sd (ddof=1). z_t is NaN until ≥ 2 finite trailing r (sd
    undefined on < 2) and NaN where the trailing sd is 0. Returns a Series on the
    input index. The intercept of the residual fit is immaterial here: z is
    invariant to adding a constant to r (mean shifts, sd unchanged)."""
    rs = pd.Series(np.asarray(r, dtype=float))
    idx = rs.index
    vals = rs.to_numpy()
    out = np.full(len(vals), np.nan, dtype=float)
    # Expanding count / mean / M2 over FINITE entries only (Welford-style so the
    # running stats use only past-and-present finite r).
    count = 0
    mean = 0.0
    m2 = 0.0
    for i in range(len(vals)):
        x = vals[i]
        if np.isfinite(x):
            count += 1
            delta = x - mean
            mean += delta / count
            m2 += delta * (x - mean)
            if count >= 2:
                var = m2 / (count - 1)        # sample variance, ddof=1
                sd = np.sqrt(var) if var > 0 else 0.0
                if sd > 0:
                    out[i] = (x - mean) / sd
                # sd == 0 (constant prefix) -> leave NaN (no crossing)
        # NaN r_t -> z_t stays NaN (this minute carries no decision)
    return pd.Series(out, index=idx)


# ==========================================================================
# 2. frozen signal — grid global-causal β + per-ticker residual
# ==========================================================================
def frozen_e1_fit(pools, insample_end=INSAMPLE_END):
    """The FROZEN E1 W=15 GLOBAL-CAUSAL (a, b), obtained by the GRID's OWN fit
    path so M3 and ``energy_grid`` cannot diverge. ``pools`` = the per-session
    pooled energy frames (``energy_grid.build_session_pools`` output). Sessions >
    insample_end are filtered INTERNALLY by ``ef.fit_global_linear`` (the
    causality invariant). Returns {'a','b','mode',...} with mode == global."""
    bundles = eg._e1_bundles(pools, FROZEN_WINDOW)
    return ef.fit_global_linear(bundles, insample_end=insample_end)


def ticker_signal(tdf, fit):
    """The per-ticker energy signal r = disp_15 − (a + b·ofi_15) using the FROZEN
    global (a, b) in ``fit``, formed on THIS ticker's own minute axis (NEVER
    pooled). Returns a Series on minute 0..389. NaN propagates where either the
    disp_15 or ofi_15 leg is NaN (or the fit is degenerate)."""
    feats = ef.compute_energy_features(tdf)
    return ef.apply_linear_residual(feats[f"disp_{FROZEN_WINDOW}"],
                                    feats[f"ofi_{FROZEN_WINDOW}"], fit)


# ==========================================================================
# 3. single-leg synthetic execution  (z-trigger; flow_policy fill/cost reused)
# ==========================================================================
def simulate_leg(bars_df, r, leg, z, p_eod_dump):
    """Simulate ONE synthetic leg on ``bars_df`` timed by the energy signal ``r``.

    ``leg`` ∈ {'buy','sell'}: buy ⇒ LONG, enters at the FIRST minute t ≥
    SCAN_START_MINUTE where the session-centered z_t ≤ −z (undershoot); sell ⇒
    SHORT, enters where z_t ≥ +z (overshoot). First crossing only.

    Decision at minute t, FILL at vw_{t+1} (the flow_policy causal fill); if the
    t+1 bar is missing/invalid OR would land past FALLBACK_MINUTE the trigger
    LAPSES and scanning continues from t+1. If no entry by FALLBACK_MINUTE (384),
    FORCED FALLBACK fills at p_eod_dump with delta_bps = 0.0 (entered AT the dump)
    — all VERBATIM from ``flow_policy``. delta_bps via ``flow_policy._delta_bps``
    (Phase-1 differential spread cost; +2bps impact cancels). Returns a dict
    {triggered, entry_minute, entry_price, used_fallback, delta_bps}."""
    if leg not in _DIRECTION_FOR_LEG:
        raise ValueError(f"leg must be one of {LEGS}, got {leg!r}")
    direction = _DIRECTION_FOR_LEG[leg]

    work, valid = ff._reindex_valid_frame(bars_df)
    vw = work["vw"]

    zser = running_z(r)
    # Align z to the 0..389 minute axis of the work-frame (r is indexed 0..389).
    zser = pd.Series(np.asarray(zser, dtype=float))
    zser.index = work.index if len(zser) == len(work.index) else zser.index

    # Forward scan for the FIRST z-crossing at minute >= SCAN_START_MINUTE; on a
    # lapse (invalid t+1 fill) continue scanning from t+1.
    t = SCAN_START_MINUTE
    while t <= FALLBACK_MINUTE - 1:        # need room for a t+1 fill
        cross_t = None
        for m in range(t, FALLBACK_MINUTE):    # m up to 383 (fill at <= 384)
            zv = float(zser.loc[m]) if m in zser.index else float("nan")
            if leg == "buy":
                hit = (zv <= -z)            # NaN -> not (NaN <= -z) is False
            else:
                hit = (zv >= z)
            if hit:
                cross_t = m
                break
        if cross_t is None:
            break

        fill_min = cross_t + 1
        if fill_min > FALLBACK_MINUTE or not bool(valid.get(fill_min, False)):
            t = cross_t + 1                # LAPSE; continue from t+1
            continue

        entry_price = float(vw.loc[fill_min])
        entry_spread = oracle.spread_bps({
            "vw": entry_price,
            "h": float(work["h"].loc[fill_min]),
            "l": float(work["l"].loc[fill_min]),
        })
        delta = fp._delta_bps(p_eod_dump, entry_price, entry_spread,
                              bars_df, direction)
        return {
            "triggered": True,
            "entry_minute": int(fill_min),
            "entry_price": entry_price,
            "used_fallback": False,
            "delta_bps": delta,
        }

    # FORCED FALLBACK — no causal entry by minute 384: fill at the dump.
    p = oracle._f(p_eod_dump)
    if not np.isfinite(p):
        return {"triggered": False, "entry_minute": None, "entry_price": None,
                "used_fallback": True, "delta_bps": float("nan")}
    return {"triggered": False, "entry_minute": None, "entry_price": float(p),
            "used_fallback": True, "delta_bps": 0.0}


# ==========================================================================
# 4. per-session simulation over both legs / all z  (cache-frame consumer)
# ==========================================================================
def simulate_session(session_bars, dump_prices, fit, z):
    """Simulate BOTH legs at threshold ``z`` for every QUALIFYING ticker in one
    session. ``session_bars`` = {ticker: bars_df}, ``dump_prices`` =
    {ticker: p_eod_dump}. Qualifying = ≥ MIN_VALID_BARS valid bars (the same gate
    as the grid). Returns {'buy': [leg_result,...], 'sell': [...]} over the
    qualifying tickers in sorted-ticker order (a ticker whose dump is None still
    runs — it falls back with delta_bps 0/NaN, honestly diluting the mean)."""
    out = {leg: [] for leg in LEGS}
    for ticker in sorted(session_bars):
        tdf = session_bars[ticker]
        if pr._valid_bar_count(tdf) < MIN_VALID_BARS:
            continue
        r = ticker_signal(tdf, fit)
        p_dump = dump_prices.get(ticker)
        for leg in LEGS:
            out[leg].append(simulate_leg(tdf, r, leg, z, p_dump))
    return out


# ==========================================================================
# 5. aggregation — per-leg per-session mean, across-session clustered t
# ==========================================================================
def _leg_session_means(per_session, leg):
    """Per-session MEAN delta_bps for one leg across its qualifying tickers (the
    SESSION is the cluster: tickers collapse to one number per session). Drops
    NaN delta_bps (unprice-able fallback). Returns {session: mean_bps} over
    sessions with ≥ 1 finite ticker delta."""
    means = {}
    for session in sorted(per_session):
        results = per_session[session].get(leg, [])
        vals = [oracle._f(res.get("delta_bps")) for res in results]
        finite = [v for v in vals if np.isfinite(v)]
        if finite:
            means[session] = float(np.mean(finite))
    return means


def _clustered_t(session_means):
    """(mean, t, n_sessions) over the per-session means with the harness
    clustered-t convention: t = mean/(sd/√n), sample sd ddof=1, n<2 -> sd/t NaN.
    The session is the cluster (never a pooled SE)."""
    vals = pd.Series(list(session_means.values()), dtype=float).dropna()
    n = int(len(vals))
    mean = float(vals.mean()) if n > 0 else float("nan")
    sd = float(vals.std(ddof=1)) if n > 1 else float("nan")
    t = mean / (sd / np.sqrt(n)) if (n > 1 and sd > 0) else float("nan")
    return mean, t, n


def _trigger_fallback_rates(per_session, leg):
    """(trigger_rate, fallback_rate) across ALL (ticker, session) pairs for one
    leg = fraction triggered / fraction fell back over every simulated pair."""
    n_pairs = 0
    n_trig = 0
    n_fall = 0
    for session in per_session:
        for res in per_session[session].get(leg, []):
            n_pairs += 1
            if res.get("triggered"):
                n_trig += 1
            if res.get("used_fallback"):
                n_fall += 1
    if n_pairs == 0:
        return float("nan"), float("nan")
    return n_trig / n_pairs, n_fall / n_pairs


def summarize_counterfactual(per_session, z):
    """Tidy summary for ONE z: a row per leg ('buy','sell') with the per-leg
    per-session-mean clustered statistics + trigger/fallback rates, PLUS a single
    'sum_diagnostic' row (the buy+sell per-session sum's clustered stats — the
    DRIFT-CONTROL diagnostic, NEVER the pass bar). ``per_session`` =
    {session: {'buy': [leg_result...], 'sell': [...]}}.

    The clustered t uses t = mean/(sd/√n), ddof=1, n<2 -> NaN; the session is the
    cluster. Returns a DataFrame with ``OUTPUT_COLUMNS``."""
    rows = []
    leg_means = {}
    for leg in LEGS:
        means = _leg_session_means(per_session, leg)
        leg_means[leg] = means
        mean, t, n = _clustered_t(means)
        trig, fall = _trigger_fallback_rates(per_session, leg)
        rows.append({
            "z": float(z), "leg": leg, "mean_bps": mean, "t": t,
            "n_sessions": n, "trigger_rate": trig, "fallback_rate": fall,
            "extra": (f"{leg} leg ⇒ {_DIRECTION_FOR_LEG[leg]}; per-session mean "
                      "delta-bps, across-session clustered t; PASS needs BOTH "
                      "legs clustered-positive"),
        })

    # SUM diagnostic: per session, buy_mean + sell_mean over the sessions where
    # BOTH legs have a finite per-session mean (the drift-control object).
    sum_means = {}
    for session in set(leg_means["buy"]) & set(leg_means["sell"]):
        sum_means[session] = leg_means["buy"][session] + leg_means["sell"][session]
    s_mean, s_t, s_n = _clustered_t(sum_means)
    rows.append({
        "z": float(z), "leg": "sum_diagnostic", "mean_bps": s_mean, "t": s_t,
        "n_sessions": s_n, "trigger_rate": float("nan"),
        "fallback_rate": float("nan"),
        "extra": "buy+sell per-session sum — DRIFT-CONTROL DIAGNOSTIC ONLY, "
                 "never the pass bar (spec §2)",
    })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def both_legs_positive(summary):
    """The spec-§2 PASS criterion for ONE z: BOTH the buy AND the sell leg
    INDIVIDUALLY clustered-positive (t > 0 with a finite, positive mean). The
    sum_diagnostic row is IGNORED (it is never the bar). NaN t (single session)
    -> not positive -> fails."""
    ok = True
    for leg in LEGS:
        sub = summary[summary["leg"] == leg]
        if sub.empty:
            return False
        row = sub.iloc[0]
        t = row["t"]
        mean = row["mean_bps"]
        if not (np.isfinite(t) and t > 0 and np.isfinite(mean) and mean > 0):
            ok = False
    return ok


# ==========================================================================
# 6. whole-grid driver — every z over the in-sample universe
# ==========================================================================
def build_counterfactual(pools, session_bars, dump_prices_by_session,
                         insample_end=INSAMPLE_END):
    """Build the full M3 tidy frame over the whole in-sample universe.

    ``pools`` = ``energy_grid.build_session_pools`` output (used ONLY to fit the
    frozen global β). ``session_bars`` = {session: {ticker: bars_df}} and
    ``dump_prices_by_session`` = {session: {ticker: p_eod_dump}} for the
    per-ticker tape simulation. Returns a tidy DataFrame: 3 z × (buy, sell,
    sum_diagnostic) = 9 rows with ``OUTPUT_COLUMNS``, in (z, leg) order. The
    frozen β is fit ONCE; every session is scored with that frozen β."""
    fit = frozen_e1_fit(pools, insample_end=insample_end)
    frames = []
    for z in Z_GRID:
        per_session = {}
        for session in sorted(session_bars):
            if session > insample_end:
                continue
            sb = session_bars[session]
            dp = dump_prices_by_session.get(session, {})
            res = simulate_session(sb, dp, fit, z)
            # keep only sessions with at least one qualifying ticker on a leg
            if res["buy"] or res["sell"]:
                per_session[session] = res
        frames.append(summarize_counterfactual(per_session, z))
    out = pd.concat(frames, ignore_index=True)
    return _sort_frame(out)


_LEG_ORDER = {"buy": 0, "sell": 1, "sum_diagnostic": 2}


def _sort_frame(frame):
    """Deterministic (z, leg) ordering so the frame / parquet is byte-stable."""
    g = frame.copy()
    g["_lo"] = g["leg"].map(lambda l: _LEG_ORDER.get(l, 99))
    g = g.sort_values(["z", "_lo"], kind="stable")
    g = g.drop(columns=["_lo"]).reset_index(drop=True)
    return g


# ==========================================================================
# 7. parquet writer + thin CACHE-ONLY driver (mirrors energy_grid)
# ==========================================================================
def write_counterfactual(frame, analysis_dir=DEFAULT_ANALYSIS_DIR):
    """Write the tidy M3 frame to ``bflow_phase1c_energy_counterfactual.parquet``.
    Returns the path. Creates the directory."""
    os.makedirs(analysis_dir, exist_ok=True)
    path = os.path.join(analysis_dir,
                        "bflow_phase1c_energy_counterfactual.parquet")
    frame.to_parquet(path, index=False)
    return path


def _load_session_bars(cache_dir, insample_end):
    """CACHE-ONLY: {session: {ticker: bars_df}} + {session: {ticker: dump}} over
    the in-sample cache sessions (same enumeration / dump as the grid). Never
    fetches. Mirrors ``energy_grid.build_session_pools`` cache plumbing."""
    sessions = p1c.enumerate_sessions(cache_dir, insample_end)
    session_bars = {}
    dump_prices = {}
    for session in sessions:
        sdf = p1c._load_session_frame(cache_dir, session)
        if sdf is None or not len(sdf):
            continue
        frames = p1c._ticker_frames(sdf)
        session_bars[session] = frames
        dump_prices[session] = {
            tk: oracle.dump_benchmark(tdf.to_dict("records"))
            for tk, tdf in frames.items()}
    return session_bars, dump_prices


def run(cache_dir=DEFAULT_CACHE_DIR, insample_end=INSAMPLE_END,
        analysis_dir=DEFAULT_ANALYSIS_DIR, progress=True):
    """Full CACHE-ONLY M3 pass: build pools (for the frozen β) -> load per-ticker
    bars -> simulate both legs over all z -> tidy frame -> parquet. Returns
    (frame, path)."""
    pools = eg.build_session_pools(cache_dir, insample_end, progress=progress)
    session_bars, dump_prices = _load_session_bars(cache_dir, insample_end)
    frame = build_counterfactual(pools, session_bars, dump_prices,
                                 insample_end=insample_end)
    path = write_counterfactual(frame, analysis_dir)
    if progress:
        print(f"[p1c-energy-M3] {len(session_bars)} sessions; "
              f"{len(frame)} rows -> {path}", flush=True)
    return frame, path


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="energy_counterfactual")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                   help="minute-bar cache dir (CACHE-ONLY; never fetched)")
    p.add_argument("--insample-end", default=INSAMPLE_END,
                   help="last in-sample session (inclusive); the global causal "
                        "frozen β uses sessions <= this (spec §2)")
    p.add_argument("--analysis-dir", default=DEFAULT_ANALYSIS_DIR,
                   help="output dir for the M3 counterfactual parquet")
    return p.parse_args(argv)


def main(argv=None):   # pragma: no cover
    args = _parse_args([] if argv is None else argv)
    frame, path = run(args.cache_dir, args.insample_end, args.analysis_dir)
    print(frame.to_string(index=False))
    print(f"[p1c-energy-M3] wrote {path}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
