"""Cluster extraction + spec input mapping (execution_signals -> spec dataclasses).

Maps our real multi-strategy ``execution_signals`` into the Ensemble Exit
Policy spec's ``Strategy[]`` + ``Context`` inputs, plus the C-slice, the short
carry, and the per-strategy half-life.

Schema adaptations vs the plan literal (verified against the live DB):
  * ``execution_signals`` day key = ``coalesce(target_date, signal_date)``
    (both DATE; mirrors /tmp/combine_backtest.py). Bracket levels:
    ``entry_price``/``stop_loss``/``target_1`` (numeric). Direction is TEXT and
    includes ``SELL_VOL``/``BUY_VOL`` (vol-leg signals) -> ``dsign`` folds them
    into +/-1 (BUY/LONG/BUY_VOL=+1, else -1; FLAT excluded upstream).
  * ``ticker_metadata_snapshots`` keys on ``symbol`` (not ``ticker``) +
    ``snapshot_date``; ``easy_to_borrow`` is a boolean (latest snapshot per
    symbol). Missing -> default True (general-collateral assumption).
  * Similarity matrix comes from ``strategy_similarity.load_groups(regime)
    ['matrix']`` -- nested dict strat->strat->[0,1], diag 1.0. Missing pairs
    default to ``DEF_OFFDIAG`` (the live sparse default).
  * Per-strategy stats come from ``strategy_weights.load_current(regime)``
    (keys: strategy_id, effective_sharpe, daily_weight, cadence_days, weight).

Eligibility divergence from combine_backtest (documented, not silent):
  combine_backtest filters legs to ``daily_weight>0`` BEFORE clustering. We
  mirror that in ``build_context`` by dropping legs whose strategy_id is absent
  from ``load_current(regime)`` or has weight<=0 (a 0-sharpe leg would poison
  the ensemble S-vector). ``extract_clusters`` itself is regime-agnostic and
  keeps all bracketed >=min_legs legs; the eligibility prune happens at
  context-build time (where the regime tables are known), and the orchestrator
  drops a cluster that falls below min_legs after the prune.
"""
from dataclasses import dataclass
import numpy as np

import exit_sim as e

DEF_OFFDIAG = 0.05  # matches strategy_similarity sparse default

# borrow assumptions (annualized) for the tiered carry arm
_BORROW_GC = 0.003   # general collateral (easy_to_borrow)
_BORROW_HTB = 0.05   # hard to borrow
_TRADING_DAYS = 252.0


def dsign(d):
    """+1 for long-side direction labels, -1 otherwise. FLAT excluded upstream."""
    return 1 if str(d).upper() in ("LONG", "BUY", "BUY_VOL") else -1


def carry_for(cluster, carry_mode, div_yield=0.0):
    """Per-bar carry (<=0) charged on a short. Longs / zero-arm -> 0.0.

    tiered: -(borrow + div)/252, borrow keyed on easy_to_borrow (GC vs HTB).
    """
    if int(cluster.direction) != -1 or carry_mode == "zero":
        return 0.0
    borrow = _BORROW_GC if getattr(cluster, "easy_to_borrow", True) else _BORROW_HTB
    return -(borrow + div_yield) / _TRADING_DAYS


def slice_C(ids, matrix):
    """N x N similarity slice for ``ids`` from a nested-dict matrix.

    Diagonal 1.0; missing off-diagonal pairs -> DEF_OFFDIAG; symmetrized.
    """
    n = len(ids)
    C = np.full((n, n), DEF_OFFDIAG, float)
    np.fill_diagonal(C, 1.0)
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j:
                continue
            v = (matrix.get(a, {}) or {}).get(b)
            if v is not None:
                C[i, j] = float(v)
    return 0.5 * (C + C.T)  # symmetrize


@dataclass
class Cluster:
    day: str
    ticker: str
    direction: int
    entry: float
    legs: list          # list[dict(strategy_id, stop_loss, target_1, target_2)]
    regime: str
    easy_to_borrow: bool


def _latest_easy_to_borrow(conn, tickers):
    """Map ticker -> easy_to_borrow from the latest snapshot per symbol.

    Missing symbol -> True (general-collateral default). Read-only.
    """
    if not tickers:
        return {}
    cur = conn.cursor()
    cur.execute(
        """select distinct on (symbol) symbol, easy_to_borrow
             from ticker_metadata_snapshots
            where symbol = any(%s)
         order by symbol, snapshot_date desc""",
        (list(tickers),),
    )
    out = {}
    for sym, etb in cur.fetchall():
        out[str(sym)] = True if etb is None else bool(etb)
    return out


def extract_clusters(conn, window_start="2026-05-04", min_legs=2):
    """Group execution_signals into same-day/ticker/direction multi-strategy
    clusters with bracket levels (read-only).

    Grouping key = (coalesce(target_date, signal_date), ticker, dsign(direction))
    over the window; drops FLAT; requires entry/stop/target_1 non-null; keeps
    groups with >= min_legs legs. ``entry`` = the top-effective_sharpe leg's
    entry_price (cluster anchor). ``regime`` = the modal regime_state of the
    cluster's legs (fallback LOW_VOL). ``easy_to_borrow`` from the latest
    ticker snapshot (True if absent).
    """
    cur = conn.cursor()
    cur.execute(
        """select coalesce(target_date, signal_date) as day, ticker, direction,
                  strategy_id, entry_price, stop_loss, target_1, target_2,
                  regime_state, effective_sharpe
             from (
                select es.*, coalesce(sw.eff, 0.0) as effective_sharpe
                  from execution_signals es
             left join (
                    select strategy_id, max(effective_sharpe) as eff
                      from strategy_weights_by_regime
                     where is_current
                  group by strategy_id
                  ) sw on sw.strategy_id = es.strategy_id
                 where es.entry_price is not null
                   and es.stop_loss is not null
                   and es.target_1 is not null
                   and es.entry_price <> 'NaN'::numeric
                   and es.stop_loss <> 'NaN'::numeric
                   and es.target_1 <> 'NaN'::numeric
                   and upper(es.direction) <> 'FLAT'
                   and coalesce(es.target_date, es.signal_date) >= %s::date
             ) q
         order by day, ticker""",
        (window_start,),
    )
    cols = [d[0] for d in cur.description]
    from collections import defaultdict, Counter
    groups = defaultdict(list)
    for r in cur.fetchall():
        s = dict(zip(cols, r))
        key = (str(s["day"]), str(s["ticker"]), dsign(s["direction"]))
        groups[key].append(s)

    # pick the entry-anchor (top effective_sharpe leg) and the modal regime
    raw_clusters = []
    tickers = set()
    for (day, ticker, dvar), legs in groups.items():
        if len(legs) < min_legs:
            continue
        anchor = max(legs, key=lambda x: float(x.get("effective_sharpe") or 0.0))
        regimes = [str(l["regime_state"]) for l in legs if l.get("regime_state")]
        regime = Counter(regimes).most_common(1)[0][0] if regimes else "LOW_VOL"
        leg_dicts = [dict(strategy_id=str(l["strategy_id"]),
                          stop_loss=float(l["stop_loss"]),
                          target_1=float(l["target_1"]),
                          target_2=(None if l.get("target_2") is None else float(l["target_2"])),
                          effective_sharpe=float(l.get("effective_sharpe") or 0.0))
                     for l in legs]
        raw_clusters.append((day, ticker, dvar, float(anchor["entry_price"]),
                             leg_dicts, regime))
        tickers.add(ticker)

    etb_map = _latest_easy_to_borrow(conn, tickers)
    clusters = []
    for day, ticker, dvar, entry, leg_dicts, regime in raw_clusters:
        clusters.append(Cluster(
            day=day, ticker=ticker, direction=int(dvar), entry=entry,
            legs=leg_dicts, regime=regime,
            easy_to_borrow=etb_map.get(ticker, True),
        ))
    return clusters


def build_context(cluster, regime_tables, conn, half_life_mode, carry_mode,
                  atr_value, txn_cost=0.0, kelly_fraction=0.5):
    """Map a Cluster -> (List[Strategy], Context) ready for generator.generate.

    ``regime_tables`` is a dict regime -> {weights, sharpe, cadence, matrix}
    (the orchestrator caches these per regime). Legs whose strategy is not in
    ``weights`` (or has weight<=0) are DROPPED (eligible-filter, mirrors
    combine_backtest). Returns (strategies, ctx, kept_leg_ids). For shorts,
    ``ctx.carry_per_bar`` is set so exit_sim_short's D2 reads it; the SAME carry
    must be passed to replay.first_touch_multiday by the caller.
    """
    import half_life as hl

    rt = regime_tables[cluster.regime]
    weights, sharpe_map = rt["weights"], rt["sharpe"]
    cadence_map, matrix = rt["cadence"], rt["matrix"]

    kept = [leg for leg in cluster.legs
            if leg["strategy_id"] in weights and float(weights[leg["strategy_id"]]) > 0]
    ids = [leg["strategy_id"] for leg in kept]

    strategies = []
    for sid in ids:
        cadence = float(cadence_map.get(sid, 1.0)) or 1.0
        hlv = hl.half_life_for(sid, conn, half_life_mode, cadence)
        strategies.append(e.Strategy(
            id=sid,
            sharpe=float(sharpe_map.get(sid, 0.0)),
            half_life=float(hlv),
            confidence=float(weights.get(sid, 0.0)),
            direction=int(cluster.direction),
        ))

    C = slice_C(ids, matrix) if ids else np.zeros((0, 0))
    ctx = e.Context(
        C=C, sigma_underlying=float(atr_value), entry_price=float(cluster.entry),
        txn_cost=float(txn_cost), hurdle_g_star=0.0,
        kelly_fraction=float(kelly_fraction), session_bars=None,
    )
    if cluster.direction == -1:
        setattr(ctx, "carry_per_bar", carry_for(cluster, carry_mode))
    return strategies, ctx, ids
