"""src/execution/option_hedge.py — SP-5.1b-ii delta-hedge ledger + target producer."""
from __future__ import annotations
import json


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
