"""Crypto pooled sizing (crypto engine Phase 3).

``OPENCLAW_CRYPTO_POOLED_SIZING`` unset ⇒ SHADOW: equity semantics are exactly
as before crypto joined the pooled sizer; ``=1`` ⇒ apply.

Pure helpers for regime_blended_sizer._sharpe_cadence_path's crypto block
(spec 2026-11-06 §5.3–§5.3c) so the big function grows by calls, not lines.
Order of operations inside the sizer's crypto block (Task 7):
  stablecoins dropped → acting gate → S_m_crypto hurdle (floor_zero) →
  pooled normaliser → dust → per-ticker cap → cluster cap → BTC corr removal →
  BTC 25 % clamp by ticker (benchmark_max_nav_frac_crypto) → long-only clamp →
  crypto_gross_cap → funding_pass (net new buying vs nmbp) → hold-flat.
"""
from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

CRYPTO = 'crypto'
EQUITY = 'equity'
# Fallback only when the crypto universe file cannot be read: stablecoins must
# never be sized as conviction names.
_STABLE_FALLBACK = frozenset({'USDC-USD', 'USDT-USD', 'USDG-USD'})


def pooled_sizing_enabled(env=os.environ) -> bool:
    """True only when ``OPENCLAW_CRYPTO_POOLED_SIZING=1`` (read at call time)."""
    return env.get('OPENCLAW_CRYPTO_POOLED_SIZING', '') == '1'


def load_crypto_regime_and_params(conn=None) -> tuple[str | None, dict | None]:
    """(crypto regime label, its regime_sizer_params row) — same table as the
    equity row, keyed by the crypto label. Fail-open: no state / any error ⇒
    (None, None) (the sizer then skips the crypto block); no params row ⇒
    {'liquidity_param': 1.0} (the equity default)."""
    try:
        from regime.crypto_regime import load_crypto_regime_state
        state = (load_crypto_regime_state() or {}).get('state')
    except Exception as e:  # noqa: BLE001
        logger.warning('crypto_sizing: crypto regime load failed (%s); crypto block skipped', e)
        return None, None
    if not state:
        return None, None
    params = None
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        try:
            import psycopg2.extras as _ex
            cur_cm = conn.cursor(cursor_factory=_ex.RealDictCursor)
        except Exception:  # noqa: BLE001 — fakes / exotic connections
            cur_cm = conn.cursor()
        with cur_cm as cur:
            cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state = %s", (state,))
            row = cur.fetchone()
        params = dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning('crypto_sizing: regime_sizer_params[%s] read failed (%s); defaults', state, e)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return state, (params or {'liquidity_param': 1.0})


def merge_weights(equity_rows: list[dict], crypto_rows: list[dict],
                  crypto_strategy_ids: set[str]) -> list[dict]:
    """Strategy weight rows: crypto ids take the crypto-regime row, every
    other id the equity-regime row (a crypto id absent from crypto_rows is
    dropped — it has no weight in its own regime)."""
    ids = set(crypto_strategy_ids or ())
    out = [r for r in (equity_rows or []) if r.get('strategy_id') not in ids]
    out += [r for r in (crypto_rows or []) if r.get('strategy_id') in ids]
    return out


def pooled_scales(*, eq_gross: float, cr_gross: float, lam_global: float,
                  liq_eq: float, liq_cr: float, nav: float) -> tuple[float, float]:
    """One λ·NAV normaliser over both classes: target_t = w_t · λ_global ·
    liq_class(t) · NAV / (Σ|w_eq| + Σ|w_cr|). With cr_gross == 0 the equity
    scale is exactly today's (λ_global·liq_eq)·NAV / Σ|w_eq|. Each class's
    liquidity_param stays a per-class dampener in [0, 1] (Reg-T bound holds)."""
    g = float(eq_gross) + float(cr_gross)
    if g <= 0:
        return 0.0, 0.0
    leq = max(0.0, min(1.0, float(liq_eq)))
    lcr = max(0.0, min(1.0, float(liq_cr)))
    return (lam_global * leq) * nav / g, (lam_global * lcr) * nav / g


def stable_tickers() -> set[str]:
    """Crypto-universe tickers with role 'stable' (all rows, active or not)."""
    try:
        from ingestion.crypto_flow.universe import load_universe
        s = {a.ticker for a in load_universe(active_only=False) if a.role == 'stable'}
        return s or set(_STABLE_FALLBACK)
    except Exception as e:  # noqa: BLE001
        logger.warning('crypto_sizing: universe read failed (%s); stablecoin fallback list', e)
        return set(_STABLE_FALLBACK)


def drop_stables(d: dict, stables: set[str]) -> tuple[dict, int]:
    """(d without stablecoin keys, number of keys dropped) — keyed by ticker."""
    kept = {k: v for k, v in (d or {}).items() if k not in stables}
    return kept, len(d or {}) - len(kept)


def crypto_gross_cap(crypto_targets: dict, nav: float, frac: float) -> tuple[dict, float]:
    """Pro-rata shave so Σ|crypto| ≤ frac·NAV; returns (targets, shaved_usd)."""
    out = dict(crypto_targets or {})
    gross = sum(abs(v) for v in out.values())
    cap = max(0.0, float(frac)) * float(nav)
    if gross <= cap or gross <= 0:
        return out, 0.0
    k = cap / gross
    return {t: v * k for t, v in out.items()}, gross - cap


def funding_pass(crypto_targets: dict, non_marginable_bp: float | None,
                 current: dict | None = None) -> tuple[dict, float | None]:
    """Alpaca crypto is non-marginable, and nmbp is cash still FREE (held
    crypto is already paid for), so the constraint is on NET NEW BUYING
    (fix round 1 ruling): demand = Σ max(target − current, 0); when demand >
    nmbp only the increases scale, k = nmbp/demand:
    target = current + (target − current)·k. Shrinking/closing targets are
    untouched. Returns (targets, shortfall = unmet new demand); nmbp None
    (unknown) ⇒ no-op, shortfall None. `current` = held crypto USD by
    internal symbol (absent ⇒ 0)."""
    out = dict(crypto_targets or {})
    if non_marginable_bp is None:
        return out, None
    try:
        bp = float(non_marginable_bp)
    except (TypeError, ValueError):
        return out, None
    if not math.isfinite(bp):
        return out, None
    bp = max(0.0, bp)
    cur = current or {}
    inc = {t: v - float(cur.get(t, 0.0) or 0.0) for t, v in out.items()}
    demand = sum(d for d in inc.values() if d > 0)
    if demand <= bp or demand <= 0:
        return out, 0.0
    k = bp / demand
    for t, d in inc.items():
        if d > 0:
            out[t] = float(cur.get(t, 0.0) or 0.0) + d * k
    return out, demand - bp


def clamp_long_only(crypto_targets: dict) -> tuple[dict, int]:
    """Alpaca crypto cannot be shorted: every crypto target ≥ 0. (targets, n_clamped)."""
    out, n = {}, 0
    for t, v in (crypto_targets or {}).items():
        if v < 0:
            out[t], n = 0.0, n + 1
        else:
            out[t] = v
    return out, n


def apply_hold_flat(crypto_targets: dict, state: dict | None) -> tuple[dict, int]:
    """Phase 2 hold-flat: a sleeve hold zeroes EVERY crypto target (sleeve
    included); a ticker hold zeroes that ticker. Returns (targets, n_zeroed)."""
    st = state or {}
    sleeve = bool(st.get('sleeve'))
    held = set((st.get('tickers') or {}).keys())
    out, n = {}, 0
    for t, v in (crypto_targets or {}).items():
        if sleeve or t in held:
            out[t] = 0.0
            n += 1
        else:
            out[t] = v
    return out, n


def _fmt_s_m(s_m) -> str:
    return 'na' if s_m is None else '%.2f' % float(s_m)


def shadow_line(crypto_regime, s_m_crypto, before: dict, after: dict, dropped: list,
                bench_tkrs: set, lam_nav: float, *, gross_cap_shaved: float,
                funding_shortfall: float | None, hold_flat: bool, mode: str,
                nav: float | None = None, stable_excluded: int = 0,
                hold_zeroed: int = 0, short_clamped: int = 0,
                eq_scale_ratio: float | None = None, btc_clamped_usd: float = 0.0) -> str:
    """One line per cycle. `before` = unconstrained crypto USD targets (post
    normaliser, pre caps); `after` = feasible targets (post caps, funding,
    hold-flat) — the shadow review sees both (spec §5.3b). `eq_scale_ratio` =
    pooled equity scale ÷ equity-only scale (the equity under-leverage the
    pooled denominator costs; crypto conviction counts pre-cap/pre-hold —
    "shave, never redistribute"); None ⇒ 'na'."""
    g0 = sum(abs(v) for v in (before or {}).values())
    g1 = sum(abs(v) for v in (after or {}).values())
    n = sum(1 for v in (after or {}).values() if v)
    pct = (' (%.1f%%NAV)' % (100.0 * g1 / nav)) if nav else ''
    fb = 'unknown' if funding_shortfall is None else '%.0f' % funding_shortfall
    top = sorted(((t, round(v)) for t, v in (after or {}).items() if v), key=lambda x: -abs(x[1]))[:8]
    return ('crypto_sizing.%s[%s]: S_m=%s n=%d gross=%.0f%s unconstrained=%.0f lam_nav=%.0f '
            'cap_shaved=%.0f funding_bind=%s hold=%d hold_zeroed=%d short_clamped=%d '
            'stable_excluded=%d eq_scale_ratio=%s btc_clamped_usd=%.0f dropped=%d bench=%s top=%s' % (
                mode, crypto_regime, _fmt_s_m(s_m_crypto), n, g1, pct, g0, lam_nav,
                gross_cap_shaved, fb, 1 if hold_flat else 0, hold_zeroed, short_clamped,
                stable_excluded, 'na' if eq_scale_ratio is None else '%.4f' % eq_scale_ratio,
                btc_clamped_usd,
                len(dropped or []), sorted(bench_tkrs or ()), top))
