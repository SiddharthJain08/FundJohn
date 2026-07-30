"""Acting-set ingest plan — scope resolver for the three-tier ingestion.

2026-07-29 operator directive (same-day execution pivot): all information
consumed by strategies ACTING in the current regime must be ingested fresh
at the ~15:00 ET pre-compute; an acting strategy must never decide on the
previous day's EOD collect. The 16:15 ET EOD collect fills the gaps and
ingests everything non-acting strategies need; a confirmed regime change
ingests only the NEW acting set's requirements delta.

This module answers "what does tier 1 (or a regime-change delta) need to
fetch": which strategies act in a regime, which data categories they
consume, and which tickers per category. Resolution deliberately reuses
the ENGINE's own selection stack — strategy_registry status='approved',
regime_gate.is_eligible (fail-open, same as the engine), and the SP-7
live-universe resolver — so ingest scope can never drift from what the
engine actually runs at signal time.

Category vocabulary matches requirements.json: prices, options_eod,
insider, financials, earnings, macro, sentiment, vol_indices,
realized_vol (computed downstream — not fetched), plus pass-through of
any future names (unknown categories surface in the plan so a new
strategy's unique requirement is VISIBLE, not silently dropped).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'

# Categories that are market-wide series (no per-ticker scoping).
MARKETWIDE_CATEGORIES = {'macro', 'vol_indices'}
# Categories derived from already-ingested data — nothing to fetch.
DERIVED_CATEGORIES = {'realized_vol', 'recent_stop_outs'}


def load_requirements(strategy_id: str) -> dict:
    """Read a strategy's requirements.json. Every live/candidate strategy
    has one (backfilled 6904b45); a missing file fails CONSERVATIVE —
    prices only, with a loud log — so a new strategy without a declaration
    still gets price data rather than nothing."""
    path = IMPL_DIR / f'{strategy_id}.requirements.json'
    try:
        data = json.loads(path.read_text())
        return {'required': list(data.get('required') or []),
                'optional': list(data.get('optional') or [])}
    except FileNotFoundError:
        logger.warning('acting_ingest_plan: %s has no requirements.json — '
                       'assuming prices-only (declare one!)', strategy_id)
        return {'required': ['prices'], 'optional': []}
    except Exception as exc:
        logger.error('acting_ingest_plan: unreadable requirements for %s '
                     '(%s) — assuming prices-only', strategy_id, exc)
        return {'required': ['prices'], 'optional': []}


class IngestPlanError(RuntimeError):
    """Raised when a plan cannot be scoped — never returned as an empty plan."""


def base_universe(cur=None) -> list[str]:
    """The set the SP-7 resolver clamps against: every ticker in master
    prices — engine parity (`engine.run_engine` logs "Universe from master
    prices: 12541" then "union 5173 tickers" after the resolver).

    The registry's `universe` column is deliberately NOT used: it holds
    symbolic labels ('SP500', 'FixedETFlist:SPY,EFA,...'), not tickers, which
    is why the engine falls through to the parquet-derived set.

    Load-bearing, not a nicety: ``build_strategy_universes`` INTERSECTS each
    predicate with this base and is documented to return ``universe ⊆
    fallback``. Called with an empty base it returns an empty universe for
    every strategy with ``error=None`` and ``adopted=True`` — a zero-ticker
    plan that looks successful (found 2026-07-30: the redeploy preflight had
    been resolving to nothing since it shipped)."""
    try:
        import pyarrow.parquet as pq
        path = ROOT / 'data' / 'master' / 'prices.parquet'
        col = pq.read_table(path, columns=['ticker']).column('ticker')
        return sorted(set(col.to_pylist()))
    except Exception as exc:  # noqa: BLE001
        logger.error('acting_ingest_plan: could not derive a base universe '
                     'from master prices (%s)', exc)
        return []


def resolve_acting_set(cur, regime_state: str) -> list[str]:
    """Strategies that will ACT under ``regime_state``: registry
    status='approved' filtered by the regime eligibility gate — the exact
    pair the engine applies at emission time (engine.load_approved_strategies
    + regime_gate.is_eligible). Weights are NOT consulted: a zero-weighted
    eligible strategy still computes signals, so its data must be fresh."""
    from strategies.regime_gate import is_eligible
    # The strategy id column is `id`, not `strategy_id` — same SELECT the
    # engine issues (engine.load_approved_strategies). The mismatch made every
    # caller fall into its non-blocking except branch, so the redeploy
    # preflight logged "failed" instead of a plan (found 2026-07-30).
    cur.execute("SELECT id FROM strategy_registry WHERE status = 'approved'")
    approved = [r['id'] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    return [sid for sid in approved if is_eligible(sid, regime_state)]


def resolve_ingest_plan(cur, regime_state: str, run_date,
                        fallback_universe: list[str] | None = None) -> dict:
    """Build the tier-1 / regime-delta ingest plan for ``regime_state``.

    Returns {
      'regime_state':  str,
      'acting':        [strategy_id, ...],
      'categories':    {category: sorted [ticker, ...]},   # per-ticker cats
      'marketwide':    sorted [category, ...],             # macro-class cats
      'consumers':     {category: [strategy_id, ...]},     # provenance
    }

    Ticker scope per category = union of the SP-7 resolver universes of the
    acting strategies that require it (optional requirements count too —
    a strategy branching on optional data still reads it when present).
    Resolver failure on a strategy fails OPEN to ``fallback_universe``
    (engine C1 semantics) so ingest scope never silently shrinks.
    """
    acting = resolve_acting_set(cur, regime_state)
    if not acting:
        return {'regime_state': regime_state, 'acting': [],
                'categories': {}, 'marketwide': [], 'consumers': {}}

    from execution.live_universe import build_strategy_universes
    fallback = list(fallback_universe or base_universe())
    if not fallback:
        raise IngestPlanError(
            'acting_ingest_plan: base universe is EMPTY — build_strategy_universes '
            'INTERSECTS its predicate with this set, so the plan would resolve to '
            'zero tickers in every category and tier-1 would ingest nothing while '
            'reporting success')
    try:
        built = build_strategy_universes(acting, run_date, fallback)
    except Exception as exc:
        logger.error('acting_ingest_plan: universe build failed wholesale '
                     '(%s) — every strategy falls open to the fallback '
                     'universe (%d tickers)', exc, len(fallback))
        built = {sid: {'universe': fallback, 'error': str(exc)} for sid in acting}

    categories: dict[str, set] = {}
    marketwide: set[str] = set()
    consumers: dict[str, list] = {}
    for sid in acting:
        reqs = load_requirements(sid)
        cats = set(reqs['required']) | set(reqs['optional'])
        uni = (built.get(sid) or {}).get('universe') or fallback
        for cat in cats:
            if cat in DERIVED_CATEGORIES:
                continue
            consumers.setdefault(cat, []).append(sid)
            if cat in MARKETWIDE_CATEGORIES:
                marketwide.add(cat)
            else:
                categories.setdefault(cat, set()).update(uni)

    for cat, tickers in categories.items():
        if not tickers:
            logger.error('acting_ingest_plan: category %s has CONSUMERS (%s) but '
                         'resolved to zero tickers — tier-1 would skip it silently',
                         cat, ','.join(consumers.get(cat, [])[:5]))

    return {
        'regime_state': regime_state,
        'acting': acting,
        'categories': {c: sorted(t) for c, t in categories.items()},
        'marketwide': sorted(marketwide),
        'consumers': {c: sorted(s) for c, s in consumers.items()},
    }


def plan_delta(new_plan: dict, fresh: dict | None) -> dict:
    """Regime-change delta: what ``new_plan`` needs that ``fresh`` (the
    categories→tickers already ingested today, e.g. by the 15:00 tier or a
    prior transition) does not cover. ``fresh=None`` → the whole plan."""
    if not fresh:
        return new_plan
    fresh_cats = {c: set(t) for c, t in (fresh.get('categories') or {}).items()}
    fresh_mkt = set(fresh.get('marketwide') or [])
    delta_categories = {}
    for cat, tickers in (new_plan.get('categories') or {}).items():
        missing = sorted(set(tickers) - fresh_cats.get(cat, set()))
        if missing:
            delta_categories[cat] = missing
    return {
        'regime_state': new_plan.get('regime_state'),
        'acting': new_plan.get('acting') or [],
        'categories': delta_categories,
        'marketwide': sorted(set(new_plan.get('marketwide') or []) - fresh_mkt),
        'consumers': new_plan.get('consumers') or {},
    }
