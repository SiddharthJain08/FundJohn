"""B1 shadow simulator (pure). Fills each plan slice at its bucket's realized VWAP
minus a conservative haircut (adverse to us), then computes the execution ledger vs
close[T+1] and the naive-3:55 baseline. Buckets without a bar are skipped (lower
completion) — never imputed. Returns floats only; raises nothing."""
from __future__ import annotations

_HAIRCUT_CAP_BPS = 50.0


def _haircut_px(bar, impact_bps):
    """Half-spread proxy (from bar high-low) + fixed impact, as a price delta."""
    v = bar.get('vwap', 0.0)
    if v <= 0:
        return 0.0
    half_spread_bps = 0.5 * (bar.get('high', v) - bar.get('low', v)) / v * 1e4
    bps = min(half_spread_bps, _HAIRCUT_CAP_BPS) + impact_bps
    return bps / 1e4 * v


def simulate(plan_slices, realized_bars, close_t1, naive_fill=None, impact_bps=2.0):
    """plan_slices: [(bucket, slice_qty)]. realized_bars: {bucket:{vwap,high,low,volume}}.
    close_t1: official close. naive_fill: actual 3:55 fill (defaults to close_t1).
    Returns {actual_fill, exec_ledger, naive_ledger, completion, filled_qty}."""
    signed_qty = sum(q for _, q in plan_slices)
    side = 1.0 if signed_qty >= 0 else -1.0   # buyer pays more, seller receives less
    filled_notional = 0.0
    filled_qty = 0.0
    for bucket, q in plan_slices:
        bar = realized_bars.get(bucket)
        if not bar or bar.get('vwap', 0.0) <= 0:
            continue
        fill_px = bar['vwap'] + side * _haircut_px(bar, impact_bps)
        filled_notional += q * fill_px
        filled_qty += q
    if filled_qty == 0:
        return {'actual_fill': None, 'exec_ledger': 0.0, 'naive_ledger': 0.0,
                'completion': 0.0, 'filled_qty': 0.0}
    actual_fill = filled_notional / filled_qty
    naive = naive_fill if naive_fill is not None else close_t1
    return {'actual_fill': actual_fill,
            'exec_ledger': (close_t1 - actual_fill) * filled_qty,
            'naive_ledger': (close_t1 - naive) * filled_qty,
            'completion': abs(filled_qty / signed_qty) if signed_qty else 0.0,
            'filled_qty': filled_qty}
