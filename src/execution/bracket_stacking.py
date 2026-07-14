#!/usr/bin/env python3
"""Block-stacked brackets — Tier-3 of strategy orthogonalization.

When multiple strategies co-fire on a ticker, combine their exit brackets instead
of picking one (the legacy regime_blended_sizer._select_bracket max-weight pick):
  * within a correlated factor block -> the top-effective-sharpe member's bracket
  * across uncorrelated blocks        -> effective-Sharpe-weighted mean of the
                                         block reps' stop/tp gaps (2026-07-14;
                                         replaced the max-take/min-stop combine).

Why the weighted mean: the merged position is the sum of sub-positions the sizer
allocates in proportion to effective Sharpe. If sub-position r (dollar size
q_r ∝ s_r) exits at level L_r, aggregate exit proceeds are Σ q_r·L_r = Q·Σ ω_r·L_r
— the single bracket that replicates the aggregate exit value is exactly the
size-weighted mean of the individual levels. All levels share one entry anchor,
so weighting pct-gaps ≡ weighting levels. Negative-Sharpe reps get zero exit
influence, matching their entry influence.

Pure: no I/O, no DB. Returns the SAME dict shape as
regime_blended_sizer._select_bracket (entry/stop/t1/t2/weight/direction) so the
downstream order builder is unchanged. {} means "no usable bracket" -> caller
falls back to _select_bracket.

Spec: docs/superpowers/specs/2026-05-29-block-stacked-brackets-design.md
      docs/superpowers/specs/2026-07-14-saturday-auto-adjustment-design.md (§W2)
"""
from __future__ import annotations
import math


def _finite(x) -> bool:
    if x is None:
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _to_fractions(b: dict, dir_sign: int):
    """(stop_pct, tp_pct) as positive fractions of entry, or None if degenerate."""
    if not (_finite(b.get('entry')) and _finite(b.get('stop')) and _finite(b.get('t1'))):
        return None
    e = float(b['entry']); s = float(b['stop']); t = float(b['t1'])
    if e <= 0:
        return None
    if dir_sign > 0:                       # long
        stop_pct = (e - s) / e
        tp_pct = (t - e) / e
    else:                                  # short
        stop_pct = (s - e) / e
        tp_pct = (e - t) / e
    if stop_pct <= 0 or tp_pct <= 0:       # inverted / degenerate
        return None
    return stop_pct, tp_pct


def _pick_top_sharpe(members: list[dict], eff_sharpe: dict[str, float]) -> dict:
    """Highest effective_sharpe member; ties broken by smallest sid (matches
    strategy_similarity.representatives determinism)."""
    return max(sorted(members, key=lambda m: str(m['sid'] or '')),
               key=lambda m: eff_sharpe.get(m['sid'], float('-inf')))


def _sharpe_weights(reps: list[dict], eff_sharpe: dict[str, float]) -> list[float]:
    """Normalized non-negative effective-Sharpe weights across block reps.
    Negative/missing Sharpe clamps to 0 (no exit influence, matching the rep's
    deflated entry influence); all-zero mass degrades to equal weights."""
    raw = []
    for r in reps:
        s = eff_sharpe.get(r['sid'])
        try:
            s = float(s)
        except (TypeError, ValueError):
            s = 0.0
        if not math.isfinite(s) or s < 0.0:
            s = 0.0
        raw.append(s)
    total = sum(raw)
    if total <= 0.0:
        return [1.0 / len(reps)] * len(reps)
    return [s / total for s in raw]


def daily_normalized_levels(entry, stop, t1, t2, cadence_days):
    """Shrink each finite bracket gap-from-entry to its single-day equivalent by
    1/sqrt(max(1, cadence_days)) — the bracket analogue of strategy_weights'
    daily_weight = effective_sharpe / sqrt(cadence_days).

    Direction-agnostic: the signed gap (level - entry) is scaled, so both longs
    and shorts shrink toward entry. Returns (stop, t1, t2). A level that is
    None/non-finite passes through unchanged; ALL levels pass through unchanged
    when `entry` is None/non-finite/<= 0 (no valid anchor to scale around).
    cadence_days is floored at 1 (a daily strategy is a no-op).
    """
    if not _finite(entry) or float(entry) <= 0:
        return stop, t1, t2
    e = float(entry)
    c = float(cadence_days or 1.0)
    if not _finite(c):
        c = 1.0
    f = 1.0 / math.sqrt(max(1.0, c))
    def _norm(x):
        return e + (float(x) - e) * f if _finite(x) else x
    return _norm(stop), _norm(t1), _norm(t2)


def stacked_bracket(brackets: list[dict], dir_sign: int,
                    block_map: dict[str, int], eff_sharpe: dict[str, float]) -> dict:
    """Combine direction-aligned contributing brackets into one stacked bracket.
    Returns {} when no usable bracket exists (caller falls back)."""
    # 1. Filter to winning direction + finite; convert to fractions of entry.
    usable: list[dict] = []
    for b in brackets:
        if b.get('direction') != dir_sign:
            continue
        fr = _to_fractions(b, dir_sign)
        if fr is None:
            continue
        stop_pct, tp_pct = fr
        usable.append({'sid': b.get('sid'), 'entry': float(b['entry']),
                       't2': b.get('t2'), 'weight': float(b.get('weight') or 0.0),
                       'stop_pct': stop_pct, 'tp_pct': tp_pct})
    if not usable:
        return {}

    # 2. Group by factor block; ungrouped sid -> its own singleton block.
    groups: dict[int, list[dict]] = {}
    local_singleton: dict[str, int] = {}
    singleton_seq = -1
    for u in usable:
        bid = block_map.get(u['sid'])
        if bid is None:
            sid_key = str(u['sid'])
            if sid_key not in local_singleton:
                local_singleton[sid_key] = singleton_seq
                singleton_seq -= 1
            bid = local_singleton[sid_key]
        groups.setdefault(bid, []).append(u)

    # 3. Per-block representative = top-effective-sharpe member.
    reps = [_pick_top_sharpe(members, eff_sharpe) for members in groups.values()]

    # 4. Combine across blocks: effective-Sharpe-weighted mean of the reps'
    #    stop/tp gaps (value replication: the single bracket that reproduces the
    #    aggregate exit value of the Sharpe-proportional sub-positions is the
    #    size-weighted mean of their levels). 2026-07-14 — replaced the former
    #    min-stop / max-tp combine.
    omega = _sharpe_weights(reps, eff_sharpe)
    stop_total = sum(w * r['stop_pct'] for w, r in zip(omega, reps))
    tp_total = sum(w * r['tp_pct'] for w, r in zip(omega, reps))

    # 5. Anchor to the highest-sharpe block rep; rebuild absolute levels.
    anchor = _pick_top_sharpe(reps, eff_sharpe)
    entry = anchor['entry']
    if dir_sign > 0:
        stop = entry * (1.0 - stop_total)
        t1 = entry * (1.0 + tp_total)
    else:
        stop = entry * (1.0 + stop_total)
        t1 = entry * (1.0 - tp_total)

    # t2: weighted mean over reps carrying a non-degenerate t2 (gap measured
    # from each rep's own entry; weights renormalized over that subset), then
    # clamped so it never inverts past the stacked t1.
    t2_terms = []
    for w, r in zip(omega, reps):
        if not (_finite(r.get('t2')) and _finite(r.get('entry'))):
            continue
        e_r = float(r['entry'])
        if e_r <= 0:
            continue
        t2_pct = (float(r['t2']) - e_r) / e_r * (1 if dir_sign > 0 else -1)
        if t2_pct <= 0:                    # wrong side of entry -> degenerate
            continue
        t2_terms.append((w, t2_pct))
    if not t2_terms:
        t2_out = None
    else:
        wsum = sum(w for w, _ in t2_terms)
        if wsum <= 0.0:                    # all mass on zero-Sharpe reps
            t2_pct_total = sum(p for _, p in t2_terms) / len(t2_terms)
        else:
            t2_pct_total = sum(w * p for w, p in t2_terms) / wsum
        if dir_sign > 0:
            t2_out = max(entry * (1.0 + t2_pct_total), t1)
        else:
            t2_out = min(entry * (1.0 - t2_pct_total), t1)
    return {
        'entry': entry, 'stop': stop, 't1': t1, 't2': t2_out,
        'weight': max(r['weight'] for r in reps),
        'direction': dir_sign, 'n_blocks': len(reps),
        'why': (f'stacked tp={tp_total:.4f} stop={stop_total:.4f} '
                f'(sharpe-wtd mean over {len(reps)} blocks)'),
    }
