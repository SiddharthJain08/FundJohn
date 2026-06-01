"""src/execution/option_hedge.py — SP-5.1b-ii delta-hedge ledger + target producer."""
from __future__ import annotations
import json
import os

# Mirror engine.py's workspace constant (resolved to UUID at runtime by caller)
WORKSPACE = os.environ.get('WORKSPACE_ID', 'default')


def _next_trading_day(d):
    """Return the next weekday after d, skipping Saturday (5) and Sunday (6).
    This is a simple calendar-math fallback; the engine uses Alpaca CLI for
    holiday-awareness, which is not available in the hedge module context."""
    import datetime as _dt
    nd = d + _dt.timedelta(days=1)
    while nd.weekday() >= 5:
        nd += _dt.timedelta(days=1)
    return nd


def upsert_hedge_target(cur, strategy_id, underlying, legs, contracts,
                        target_hedge_qty, as_of):
    """Upsert the ledger row's target_hedge_qty (the EOD-computed hedge), keyed by
    (option_strategy_id, underlying). current_hedge_qty is NOT touched here (it
    updates from real fills — see update_hedge_filled)."""
    cur.execute(
        """INSERT INTO option_hedge_ledger
             (option_strategy_id, underlying, structure_legs, contracts,
              target_hedge_qty, last_rehedge_date, status, updated_at)
           VALUES (%s,%s,%s::jsonb,%s,%s,%s,'active',NOW())
           ON CONFLICT (option_strategy_id, underlying) DO UPDATE SET
             structure_legs=EXCLUDED.structure_legs, contracts=EXCLUDED.contracts,
             target_hedge_qty=EXCLUDED.target_hedge_qty,
             last_rehedge_date=EXCLUDED.last_rehedge_date,
             status='active', updated_at=NOW()""",
        (strategy_id, underlying, json.dumps(legs), contracts,
         target_hedge_qty, as_of))


def load_active_hedges(cur):
    """All active ledger rows as dicts."""
    cur.execute("""SELECT option_strategy_id, underlying, structure_legs, contracts,
                          current_hedge_qty, target_hedge_qty
                   FROM option_hedge_ledger WHERE status='active'""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def hedge_qty_by_underlying(cur):
    """{underlying: sum current_hedge_qty} across active hedges — the amount the
    production sizer must EXCLUDE from the broker book."""
    cur.execute("""SELECT underlying, COALESCE(SUM(current_hedge_qty),0)
                   FROM option_hedge_ledger WHERE status='active' GROUP BY underlying""")
    return {u: float(q) for u, q in cur.fetchall()}


def close_hedge(cur, strategy_id, underlying):
    """Mark closed + zero the target (next EOD emits a flatten-to-0 hedge target)."""
    cur.execute("""UPDATE option_hedge_ledger SET status='closed', target_hedge_qty=0,
                   updated_at=NOW() WHERE option_strategy_id=%s AND underlying=%s""",
                (strategy_id, underlying))


def _leg_delta(occ, right, strike, expiry):
    """Live delta for one option leg, from the chain at its exact strike. Reuses
    SP-5.1b-i's _option_chain_greeks (snapshots[occ].greeks.delta). Returns the
    matched OCC's delta, or None if unavailable (fail-closed upstream)."""
    import datetime as _dt
    from execution.alpaca_executor import _option_chain_greeks, _spot_price
    und = ''.join(c for c in occ if c.isalpha())
    spot = _spot_price(und)
    if spot is None or spot <= 0:
        return None
    exp = _dt.date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    # band wide enough to include this strike (its distance from spot) + margin
    band = max(0.02, abs(float(strike) - spot) / spot + 0.005)
    for (k, d, o) in _option_chain_greeks(und, exp, right, spot, band_pct=band):
        if o == occ:
            return float(d)
    return None


def compute_structure_delta(legs, contracts):
    """Net share-delta of the long structure = Σ(leg delta) × 100 × contracts.
    Returns None (fail-closed) if ANY leg delta can't be resolved — so the hedge
    target producer emits no (wrong) hedge for that structure."""
    total = 0.0
    for leg in legs:
        d = _leg_delta(leg['occ'], leg['right'], leg['strike'], leg.get('expiry'))
        if d is None:
            return None
        total += d
    return total * 100.0 * float(contracts)


def compute_option_hedge_targets(cur, as_of, workspace_id=None):
    """For each active option_hedge_ledger row, compute the net structure delta and
    inject a lifecycle_state='APPROVED' execution_signals row (gate-bypass) carrying
    a fixed hedge_shares target.  Also upserts the ledger's target_hedge_qty.

    Design:
      - net = compute_structure_delta(legs, contracts)
      - If net is None → skip (fail-closed, no hedge row emitted for that structure)
      - target_shares = -net  (hedge offsets the structure's delta)
      - If target_shares == 0 → skip (perfectly delta-neutral; no hedge row needed)
      - direction = 'LONG' if target_shares > 0 else 'SHORT'
      - Writes execution_signals: strategy_id = '__hedge__<option_sid>',
        lifecycle_state='APPROVED', approved_at=NOW() (gate-bypass — risk hedge
        must not be panic/sentiment-vetoed), signal_params carries is_hedge + hedge_shares
      - workspace_id: callers (run_option_hedge_targets.py) should resolve and pass the
        same UUID that engine.write_signals writes — i.e. resolve_workspace(cur, WORKSPACE).
        When None, we mirror engine.main()'s resolution: resolve_workspace(cur, WORKSPACE)
        using the module-level WORKSPACE constant (identical bare string os.environ
        WORKSPACE_ID / 'default').  This guarantees workspace-scoped consumers (e.g. the
        sizer, parity step) see the hedge rows — writing None or bare 'default' would
        silently drop them from workspace-filtered queries.
        engine.write_signals writes: resolve_workspace(cur, WORKSPACE) → a 36-char UUID
        from workspaces.id.  This function now writes the IDENTICAL resolved UUID.
      - ON CONFLICT DO UPDATE (hedge targets may change each EOD cycle)
      - Upserts ledger target_hedge_qty = target_shares via upsert_hedge_target()
    """
    from datetime import datetime, timezone
    from execution.engine import resolve_workspace

    rows = load_active_hedges(cur)
    target_date = _next_trading_day(as_of)

    # Resolve workspace_id once — mirrors engine.main()'s WORKSPACE = resolve_workspace(cur, WORKSPACE).
    # The resolved value is a 36-char UUID from workspaces.id (NOT 'default', NOT None).
    if workspace_id is None:
        workspace_id = resolve_workspace(cur, WORKSPACE)

    for row in rows:
        option_sid  = row['option_strategy_id']
        underlying  = row['underlying']
        legs        = row['structure_legs']
        contracts   = row['contracts']

        net = compute_structure_delta(legs, contracts)
        if net is None:
            # Fail-closed: can't determine delta → emit no hedge row
            continue

        target_shares = -net
        if target_shares == 0:
            # Perfectly delta-neutral structure — no hedge needed; skip to avoid
            # emitting a SHORT-0 row that would confuse the executor.
            continue

        direction = 'LONG' if target_shares > 0 else 'SHORT'
        hedge_strategy_id = f'__hedge__{option_sid}'

        signal_params = json.dumps({
            'is_hedge': True,
            'hedge_strategy_id': option_sid,
            'hedge_underlying': underlying,
            'hedge_shares': abs(target_shares),
        })

        now_utc = datetime.now(timezone.utc)

        cur.execute("""
            INSERT INTO execution_signals
                (strategy_id, workspace_id, signal_date, ticker, direction,
                 entry_price, stop_loss, target_1, target_2, target_3,
                 position_size_pct, regime_state, signal_params, status,
                 lifecycle_state, computed_at, target_date, approved_at)
            VALUES (%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                    %s::jsonb,'open','APPROVED',%s,%s,%s)
            ON CONFLICT (strategy_id, signal_date, ticker, direction) DO UPDATE SET
                signal_params=EXCLUDED.signal_params,
                lifecycle_state='APPROVED',
                target_date=EXCLUDED.target_date,
                approved_at=EXCLUDED.approved_at
        """, (
            hedge_strategy_id, workspace_id, as_of, underlying, direction,
            signal_params, now_utc, target_date, now_utc,
        ))

        # Upsert the ledger's target (pass option_sid, not hedge_sid)
        upsert_hedge_target(cur,
                            strategy_id=option_sid,
                            underlying=underlying,
                            legs=legs,
                            contracts=contracts,
                            target_hedge_qty=target_shares,
                            as_of=as_of)
