"""src/execution/deterministic_sizer.py

Deterministic position sizer. Replaces TradeJohn the LLM with a pure
Python function that produces the same sized-handoff JSON the LLM was
expected to emit. The math mirrors the rules in
`src/agent/prompts/subagents/tradejohn.md` and the static knobs in
`src/plugins/fundjohn/skills/position-sizer/SKILL.md` — except where
those documents disagree with the deterministic upstream pipeline, the
upstream pipeline wins (see notes below).

Inputs the sizer consumes:

  handoff.regime.scale          — authoritative regime multiplier
  handoff.signals[]             — green-prefiltered signals; each carries
                                   ticker, strategy_id, direction, entry,
                                   stop, t1, p_t1, ev_gbm, plus optional
                                   d1 = {kind, sigma_delta, reason}.
  handoff.d1_strategy_stats     — {strategy_id: {overperf, underperf, rejected}}
  handoff.portfolio.portfolio_value — NAV for share-count math
  manifest.strategies[sid].state — lifecycle gate

Inputs explicitly NOT consumed:

  handoff.signals[].size_pct    — strategy-author preferred size, NOT
                                   Kelly. Recomputed here from p_t1+R.
  handoff.signals[].confidence  — string label (MED/HIGH); use p_t1.
  handoff.mastermind_rec        — Saturday-brain recommendations. Not
                                   wired into sizing yet — would need a
                                   separate per-strategy override table.
  handoff.signals[].confluence_count — not in current handoff schema.
                                       Confluence bonus is therefore
                                       skipped; signals get only their
                                       own Kelly + regime + d-1 sizing.

Computation order (per signal):

  0. Lifecycle gate — manifest state in {'candidate','staging'} → veto.
  1. Rule A — repeat-offender veto on d1.reason.
  2. Half-Kelly:  R = (t1-entry)/(entry-stop) for LONG/BUY*;
                  f_star = (p_t1*R - (1-p_t1)) / R; veto if f_star ≤ 0.
                  pct_nav = clip(0.5 * f_star, 0, MAX_POSITION_PCT).
  3. Rule C — d1.kind=='over'  → ×1.2.
  4. Rule E — d1.kind=='under' → ×0.7.
  5. Rule B — d1_strategy_stats[sid].rejected ≥ 5  → ×0.7.
  6. Rule F — d1_strategy_stats[sid].underperf ≥ 3 → ×0.8.
  7. Regime  — × handoff.regime.scale.
  8. Lifecycle — × lifecycle_mult.
  9. Hard cap  — min(MAX_POSITION_PCT).
 10. Floor    — pct_nav < MIN_EFFECTIVE_PCT → veto below_min_effective_pct.
 11. shares   — floor(pct_nav * NAV / entry).

Portfolio cap (poker-bankroll, applied after per-signal sizing):

  if sum(pct_nav) > MAX_DAILY_NEW_NOTIONAL_PCT:
     drop lowest-EV orders into vetoed[reason='daily_notional_cap']
     until total ≤ cap.

Schema parity with `tradejohn_orders` JSON block — any consumer that
read the LLM's output reads this output unchanged.
"""
from __future__ import annotations

import math
from typing import Any


# ── Constants ────────────────────────────────────────────────────────────
# These mirror src/execution/alpaca_executor.py:47–49 and
# src/agent/prompts/subagents/tradejohn.md. Kept in one place here so the
# math is testable and the prompt can be deleted later.

MAX_POSITION_PCT          = 0.05    # 5% NAV max per signal
MIN_EFFECTIVE_PCT         = 0.001   # 0.1% NAV floor; below this we skip
MAX_DAILY_NEW_NOTIONAL_PCT = 0.25   # 25% NAV daily aggregate cap
HALF_KELLY                = 0.5     # always half-Kelly per SKILL.md

# Rule allowlist for Rule A (repeat-offender veto). Only these reasons
# carry over from prior cycles as auto-vetoes — other rejection causes
# (e.g. data gaps) shouldn't suppress today's signal.
RULE_A_REASONS = {
    'prefilter_negative_ev',
    'negative_kelly',
    'kelly_below_threshold',
}

# Rules C/E multipliers (per-signal d-1 outcome).
OVERPERF_BONUS    = 1.2
UNDERPERF_PENALTY = 0.7

# Rules B/F multipliers (strategy-wide d-1 aggregates).
STRATEGY_REJECTED_THRESHOLD  = 5
STRATEGY_REJECTED_MULTIPLIER = 0.7
STRATEGY_UNDER_THRESHOLD     = 3
STRATEGY_UNDER_MULTIPLIER    = 0.8

# Lifecycle multipliers — applied AFTER regime scale. `candidate` and
# `staging` are hard-vetoed (multiplier 0.0) since they shouldn't be
# placing live orders. `paper`/`monitoring` half-size as soft canary.
# Default for unknown states is 0.0 — fail closed.
LIFECYCLE_MULTIPLIER: dict[str, float] = {
    'live':       1.0,
    'paper':      0.5,
    'monitoring': 0.5,
    'candidate':  0.0,
    'staging':    0.0,
}


# ── Per-signal math ──────────────────────────────────────────────────────

def _reward_to_risk(direction: str, entry: float, stop: float, t1: float) -> float | None:
    """Compute reward-to-risk ratio. Returns None for malformed signals.

    LONG / BUY / BUY_VOL: stop below entry, target above. R = (t1-entry)/(entry-stop).
    SHORT / SELL / SELL_VOL: stop above entry, target below. R = (entry-t1)/(stop-entry).
    Mixed-up bracket prices return None (Rule: malformed_signal_R<=0 → veto).
    """
    d = (direction or '').upper()
    is_long = d in ('LONG', 'BUY', 'BUY_VOL') or d.startswith('BUY')
    if is_long:
        if entry <= stop:
            return None
        return (t1 - entry) / (entry - stop)
    # short side
    if stop <= entry:
        return None
    return (entry - t1) / (stop - entry)


def _kelly_fraction(p: float, R: float) -> float:
    """Raw Kelly: f* = (p*R - (1-p)) / R. Negative when EV < 0."""
    if R <= 0:
        return 0.0
    return (p * R - (1.0 - p)) / R


def _veto(ticker: str, strategy_id: str, direction: str,
          reason: str, ev: float | None = None,
          extra: dict | None = None) -> dict:
    out = {'ticker': ticker, 'strategy_id': strategy_id,
           'direction': (direction or '').lower(),
           'reason': reason}
    if ev is not None:
        out['ev'] = ev
    if extra:
        out.update(extra)
    return out


def _size_one(signal: dict, regime_scale: float, lifecycle_mult: float,
              d1_strategy_stats: dict, nav: float) -> tuple[dict | None, dict | None]:
    """Return (order, veto). Exactly one of the two is None."""
    sid    = signal.get('strategy_id') or ''
    ticker = signal.get('ticker') or ''
    direction = signal.get('direction') or 'LONG'
    entry  = float(signal.get('entry')   or 0)
    stop   = float(signal.get('stop')    or 0)
    t1     = float(signal.get('t1')      or 0)
    p_t1   = float(signal.get('p_t1')    or 0)
    ev_gbm = signal.get('ev_gbm')
    d1     = signal.get('d1') or {}

    if entry <= 0 or stop <= 0 or t1 <= 0 or p_t1 <= 0:
        return None, _veto(ticker, sid, direction, 'malformed_signal_zero_field', ev_gbm)

    # 0. Lifecycle gate — hard-veto for states that can't trade live.
    if lifecycle_mult <= 0.0:
        # The state name was already plumbed into the multiplier; the
        # caller stamps a more specific reason ('lifecycle_candidate' etc.)
        return None, _veto(ticker, sid, direction, 'lifecycle_blocked', ev_gbm)

    # 1. Rule A — repeat-offender veto.
    if d1.get('kind') == 'rejected' and d1.get('reason') in RULE_A_REASONS:
        return None, _veto(ticker, sid, direction, 'repeat_offender_d-1', ev_gbm,
                           extra={'d1_reason': d1.get('reason')})

    # 2. Reward-to-risk + raw Kelly.
    R = _reward_to_risk(direction, entry, stop, t1)
    if R is None or R <= 0:
        return None, _veto(ticker, sid, direction, 'malformed_signal_R<=0', ev_gbm)
    f_star = _kelly_fraction(p_t1, R)
    if f_star <= 0:
        return None, _veto(ticker, sid, direction, 'negative_kelly', ev_gbm)

    pct_nav = HALF_KELLY * f_star
    # Clip to MAX_POSITION_PCT BEFORE applying multipliers so the
    # multipliers can only reduce. This matches the SKILL.md ordering
    # and prevents a wildly overstated raw Kelly from amplifying a d-1
    # bonus past the cap.
    pct_nav = min(pct_nav, MAX_POSITION_PCT)

    # 3. Rule C — overperformance bonus.
    if d1.get('kind') == 'over':
        pct_nav *= OVERPERF_BONUS

    # 4. Rule E — underperformance penalty.
    if d1.get('kind') == 'under':
        pct_nav *= UNDERPERF_PENALTY

    # 5/6. Rules B + F — strategy-wide aggregates.
    ds = (d1_strategy_stats or {}).get(sid) or {}
    if int(ds.get('rejected') or 0) >= STRATEGY_REJECTED_THRESHOLD:
        pct_nav *= STRATEGY_REJECTED_MULTIPLIER
    if int(ds.get('underperf') or 0) >= STRATEGY_UNDER_THRESHOLD:
        pct_nav *= STRATEGY_UNDER_MULTIPLIER

    # 7. Regime scale (from handoff — authoritative).
    pct_nav *= float(regime_scale or 0.0)

    # 8. Lifecycle scale.
    pct_nav *= lifecycle_mult

    # 9. Final hard cap (defensive — multipliers should only reduce).
    pct_nav = min(pct_nav, MAX_POSITION_PCT)

    # 10. Sub-noise floor.
    if pct_nav < MIN_EFFECTIVE_PCT:
        return None, _veto(ticker, sid, direction, 'below_min_effective_pct', ev_gbm,
                           extra={'pct_nav_pre_floor': round(pct_nav, 5)})

    # 11. Shares + notional.
    shares = int(math.floor(pct_nav * nav / entry)) if entry > 0 else 0
    if shares <= 0:
        return None, _veto(ticker, sid, direction, 'below_min_share_count', ev_gbm,
                           extra={'pct_nav': round(pct_nav, 5)})

    order = {
        'ticker':       ticker,
        'strategy_id':  sid,
        'direction':    (direction or '').lower(),
        'entry':        entry,
        'stop':         stop,
        't1':           t1,
        't2':           signal.get('t2'),
        'pct_nav':      round(pct_nav, 5),
        'shares':       shares,
        'notional_usd': round(pct_nav * nav, 2),
        'kelly_final':  round(pct_nav, 5),
        'ev':           ev_gbm,
        'p_t1':         p_t1,
    }
    return order, None


# ── Top-level entry ──────────────────────────────────────────────────────

def size_orders(handoff: dict, manifest: dict | None = None,
                nav: float | None = None) -> dict:
    """Apply the full deterministic sizing pipeline. Pure function — no
    DB, no I/O, no LLM. Takes the structured-handoff dict (as
    `trade_agent_llm.py` already builds it via the whitelist) and the
    strategy manifest dict. Returns the sized handoff payload, schema-
    identical to the LLM's prior `tradejohn_orders` JSON block.

    `nav` defaults to `handoff.portfolio.portfolio_value` if available,
    else 1_000_000 (paper-trading default).
    """
    portfolio = handoff.get('portfolio') or {}
    if nav is None:
        nav = float(portfolio.get('portfolio_value') or 1_000_000)

    regime_obj = handoff.get('regime') or {}
    regime_state = regime_obj.get('state') if isinstance(regime_obj, dict) else 'TRANSITIONING'
    regime_scale = float(regime_obj.get('scale')
                         if isinstance(regime_obj, dict) else 0.55)

    d1_stats = handoff.get('d1_strategy_stats') or {}
    strategies = (manifest or {}).get('strategies') or {}

    orders: list[dict] = []
    vetoed: list[dict] = []

    for sig in handoff.get('signals') or []:
        sid = sig.get('strategy_id') or ''
        state = (strategies.get(sid) or {}).get('state', 'live')
        lifecycle_mult = LIFECYCLE_MULTIPLIER.get(state, 0.0)

        order, veto = _size_one(sig, regime_scale, lifecycle_mult, d1_stats, nav)
        if order is not None:
            orders.append(order)
        else:
            # Re-stamp the lifecycle-blocked reason with the actual state name.
            if veto and veto.get('reason') == 'lifecycle_blocked':
                veto['reason'] = f'lifecycle_{state}'
            vetoed.append(veto)

    # Portfolio-level "poker bankroll" cap. The aggregate notional across
    # all new positions for the day cannot exceed MAX_DAILY_NEW_NOTIONAL_PCT.
    #
    # Strategy: pro-rata scale every surviving order down by the same
    # factor when the raw sum exceeds the cap. This preserves each
    # signal's *relative* weight — high-EV signals still get bigger
    # slices than low-EV ones — but sizes ALL of them, instead of
    # dropping the long tail.
    #
    # After the scale-down, some orders may fall below MIN_EFFECTIVE_PCT
    # (the noise floor). Those get vetoed individually with reason
    # 'below_min_effective_post_scale'. We then recompute totals; the
    # second pass is bounded since each scale-down can only reduce the
    # set, not grow it.
    raw_total = sum(float(o.get('pct_nav') or 0) for o in orders)
    if raw_total > MAX_DAILY_NEW_NOTIONAL_PCT and raw_total > 0:
        scale = MAX_DAILY_NEW_NOTIONAL_PCT / raw_total
        for o in orders:
            o['pct_nav_pre_scale'] = o['pct_nav']
            o['pct_nav']      = round(o['pct_nav'] * scale, 6)
            o['notional_usd'] = round(o['pct_nav'] * nav, 2)
            o['kelly_final']  = o['pct_nav']
        # Drop anything that fell below the noise floor or below 1 share.
        kept: list[dict] = []
        for o in orders:
            if o['pct_nav'] < MIN_EFFECTIVE_PCT:
                vetoed.append(_veto(
                    o['ticker'], o['strategy_id'], o['direction'],
                    'below_min_effective_post_scale', o.get('ev'),
                    extra={'pct_nav_pre_scale': o.get('pct_nav_pre_scale'),
                           'pct_nav_post_scale': o['pct_nav']},
                ))
                continue
            shares = int(math.floor(o['pct_nav'] * nav / o['entry'])) if o['entry'] > 0 else 0
            if shares <= 0:
                vetoed.append(_veto(
                    o['ticker'], o['strategy_id'], o['direction'],
                    'below_min_share_count_post_scale', o.get('ev'),
                    extra={'pct_nav_post_scale': o['pct_nav']},
                ))
                continue
            o['shares'] = shares
            kept.append(o)
        orders = kept

    # Rank surviving orders by EV × pct_nav descending — highest expected
    # contribution first. Decorative for dashboards; downstream
    # alpaca_executor processes orders in this order so the per-cycle
    # cumulative-notional check (alpaca_executor.MAX_DAILY_NEW_NOTIONAL_PCT)
    # admits the highest-EV first.
    orders.sort(key=lambda o: float(o.get('ev') or 0) * float(o.get('pct_nav') or 0),
                reverse=True)
    for i, o in enumerate(orders, 1):
        o['priority_rank'] = i

    return {
        'cycle_date':     handoff.get('cycle_date'),
        'regime':         regime_state,
        'regime_scale':   regime_scale,
        'nav':            nav,
        'total_green':    len(orders),
        'total_vetoed':   len(vetoed),
        'orders':         orders,
        'vetoed':         vetoed,
    }


if __name__ == '__main__':
    # Tiny CLI for ad-hoc inspection: pass a structured-handoff JSON path,
    # prints the sized payload to stdout.
    import json
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print('usage: deterministic_sizer.py <structured-handoff.json>',
              file=sys.stderr)
        sys.exit(2)
    handoff = json.loads(Path(sys.argv[1]).read_text())
    manifest_path = Path(__file__).resolve().parents[2] / 'src' / 'strategies' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    print(json.dumps(size_orders(handoff, manifest), indent=2, default=str))
