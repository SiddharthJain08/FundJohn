#!/usr/bin/env python3
"""
trade_handoff_builder.py — deterministic feature builder for TradeJohn.

Replaces research_report.py's signal-enrichment pass. Reads signals from
execution_signals for the run_date, computes pure-Python features (HV,
beta, momentum, GBM first-passage EV / p_t1, RSI), and writes a compact
structured JSON handoff that TradeJohn consumes directly.

No LLM, no Discord posts, no markdown. Size target: ≤ 30KB per cycle so
TradeJohn's input stays well under the 200K context window even at 500+
signals (avoiding the budget blowout that killed the 2026-04-22 catch-up).

Usage:
    python3 src/execution/trade_handoff_builder.py --date YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.handoff import write_handoff, read_handoff  # noqa: E402

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_DAILY       = 0.05 / TRADING_DAYS_PER_YEAR

# Pre-filter: only signals clearing these gates are sent to TradeJohn.
# Anything that fails here gets dropped to `prefiltered` with the reason —
# those bubble into send_report.py's veto digest as a separate reason
# bucket. Goal: keep TradeJohn's context focused on signals with real
# positive-Kelly potential so it can spend its token budget on sizing
# nuance instead of re-rejecting the same negative-EV signals every day.
MIN_EV_GBM   = 0.005   # 0.5% expected value per trade — anything below
                       # is below the friction+slippage floor for live
                       # execution and never survives TradeJohn's gate
                       # anyway. Empirically 99% of 04-23 vetoes were
                       # below this line.
MIN_P_T1     = 0.30    # target must be at least somewhat reachable;
                       # strategies that report p_t1 < 30% are almost
                       # always below the Kelly-viable boundary
                       # (R/(R+1) ≈ 0.4 for R=1.5).

# Per-strategy expected holding-period lookup. Keep in sync with the old
# research_report; defaults cover any strategy not listed.
_HP_OPTIONS = {'min': 1, 'target': 5, 'max': 21}
HOLDING_PERIOD = {
    'S9_dual_momentum':               {'min': 21, 'target': 63,  'max': 126},
    'S_custom_jt_momentum_12mo':      {'min': 42, 'target': 105, 'max': 189},
    'S10_quality_value':              {'min': 21, 'target': 63,  'max': 126},
    'S12_insider':                    {'min': 10, 'target': 30,  'max': 63},
    'S15_iv_rv_arb':                  {'min': 1,  'target': 10,  'max': 21},
    'S_HV13_call_put_iv_spread':      _HP_OPTIONS,
    'S_HV14_otm_skew_factor':         _HP_OPTIONS,
    'S_HV15_iv_term_structure':       _HP_OPTIONS,
    'S_HV16_gex_regime':              _HP_OPTIONS,
    'S_HV17_earnings_straddle_fade':  {'min': 1, 'target': 3, 'max': 7},
    'S_HV19_iv_surface_tilt':         _HP_OPTIONS,
    'S_HV20_iv_dispersion_reversion': _HP_OPTIONS,
}
DEFAULT_HP = {'min': 1, 'target': 5, 'max': 21}


# ── Loaders ────────────────────────────────────────────────────────────────

def load_signals(uri: str, run_date: str) -> list[dict]:
    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT strategy_id, ticker, direction, entry_price, stop_loss,
               target_1, target_2, target_3, position_size_pct,
               signal_params, regime_state
          FROM execution_signals
         WHERE signal_date = %s
         ORDER BY strategy_id, ticker
        """,
        (run_date,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        for col in ('entry_price','stop_loss','target_1','target_2','target_3','position_size_pct'):
            v = d.get(col)
            if v is not None:
                d[col] = float(v)
        if isinstance(d.get('signal_params'), str):
            try:
                d['signal_params'] = json.loads(d['signal_params']) if d['signal_params'] else {}
            except ValueError:
                d['signal_params'] = {}
        # Confidence lives in signal_params, not in its own column. Default MED.
        params = d.get('signal_params') or {}
        d['confidence'] = (params.get('confidence') if isinstance(params, dict) else None) or 'MED'
        rows.append(d)
    conn.close()
    return rows


def load_prices() -> pd.DataFrame:
    p = ROOT / 'data/master/prices.parquet'
    df = pd.read_parquet(p)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['ticker', 'date'])
    return df.pivot(index='date', columns='ticker', values='close').sort_index()


def load_regime(uri: str) -> dict:
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT state, stress_score, timestamp FROM regime_states ORDER BY timestamp DESC LIMIT 1"
        )
        r = cur.fetchone()
        conn.close()
        if r:
            state = r.get('state') or 'TRANSITIONING'
            return {
                'state':   state,
                'stress':  float(r.get('stress_score') or 0),
                'scale':   {'LOW_VOL':1.0,'TRANSITIONING':0.55,'HIGH_VOL':0.35,'CRISIS':0.15}.get(state, 0.55),
            }
    except Exception:
        pass
    # Fallback: read workspaces/default/regime.json
    rj = ROOT / 'workspaces' / 'default' / 'regime.json'
    if rj.exists():
        try:
            j = json.loads(rj.read_text())
            return {'state': j.get('state', 'TRANSITIONING'),
                    'stress': float(j.get('stress', 0)),
                    'scale':  float(j.get('position_scale', 0.55))}
        except Exception:
            pass
    return {'state': 'TRANSITIONING', 'stress': 50.0, 'scale': 0.55}


def load_portfolio_state() -> dict:
    p = ROOT / 'output' / 'portfolio.json'
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def load_veto_history(uri: str, days: int = 30) -> dict:
    if not uri:
        return {}
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        cur.execute(
            """
            SELECT strategy_id, veto_reason, COUNT(*) AS n
              FROM veto_log
             WHERE run_date >= %s AND strategy_id IS NOT NULL
             GROUP BY strategy_id, veto_reason
             ORDER BY strategy_id, n DESC
            """,
            (cutoff,),
        )
        hist: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            hist.setdefault(row['strategy_id'], {})[row['veto_reason']] = int(row['n'])
        conn.close()
        return hist
    except Exception as e:
        print(f'[handoff] veto history unavailable: {e}')
        return {}


def _previous_trading_day(run_date: str) -> str:
    """Previous weekday in YYYY-MM-DD form. Skips Sat/Sun but not market
    holidays — missing files on holiday-shifted runs simply return empty
    lists (callers are defensive)."""
    from datetime import date as _d, timedelta as _td
    d = _d.fromisoformat(run_date) - _td(days=1)
    while d.weekday() >= 5:
        d -= _td(days=1)
    return d.isoformat()


def load_yesterdays_vetoed(run_date: str) -> list[dict]:
    """Read the prior cycle's sized handoff and surface its vetoed list
    verbatim — reasons, EV, p_t1 intact. Lets TradeJohn catch repeat
    offenders: signals still reported green by a strategy today but which
    the joint pipeline (prefilter + TradeJohn) rejected yesterday."""
    yesterday = _previous_trading_day(run_date)
    sized = read_handoff(yesterday, 'sized') or {}
    return sized.get('vetoed') or []


def load_yesterdays_performance_outliers(uri: str, run_date: str) -> tuple[list[dict], list[dict]]:
    """Cross-reference yesterday's structured handoff (for each signal's
    original ev_gbm + hv21) against signal_pnl (for realized or unrealized
    outcomes). Per signal we compute:

        sigma_holding = hv21 × sqrt(days_held / 252)   # GBM vol over the
                                                       # actual holding period
        delta         = actual_return - ev_gbm
        sigma_delta   = delta / sigma_holding          # standardized surprise

    Outliers are signals where |sigma_delta| ≥ SIGMA_GATE (currently 1.0).
    Every candidate clearing the gate is returned — no row cap. Tuning
    happens via the gate itself. Returns (overperformers, underperformers),
    each sorted by |sigma_delta| desc."""
    import math as _math
    if not uri:
        return [], []
    yesterday = _previous_trading_day(run_date)
    y_struct = read_handoff(yesterday, 'structured') or {}

    # Index yesterday's enriched signals by (ticker, strategy_id) so we
    # can recover ev_gbm + hv21 (the σ source) for each signal_pnl row.
    ev_lookup: dict[tuple, dict] = {}
    for s in (y_struct.get('signals') or []) + (y_struct.get('prefiltered') or []):
        ev = s.get('ev_gbm')
        if ev is None:
            continue
        ev_lookup[(s.get('ticker'), s.get('strategy_id'))] = {
            'ev_gbm': float(ev),
            'hv21':   float(s.get('hv21') or 0.0),
            'entry':  s.get('entry'),
        }
    if not ev_lookup:
        return [], []

    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT sp.signal_id, sp.strategy_id, sp.pnl_date, sp.close_price,
                   sp.unrealized_pnl_pct, sp.realized_pnl_pct, sp.days_held,
                   sp.status, sp.close_reason,
                   es.ticker, es.direction, es.entry_price
              FROM signal_pnl sp
              JOIN execution_signals es ON es.id = sp.signal_id
             WHERE sp.pnl_date = %s
            """,
            (yesterday,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f'[handoff] performance-outlier query failed: {e}')
        return [], []

    overperformers: list[dict] = []
    underperformers: list[dict] = []
    SIGMA_GATE = 1.0
    SIGMA_FLOOR = 0.005   # protect against near-zero hv → huge sigma_delta

    for row in rows:
        key = (row['ticker'], row['strategy_id'])
        y_entry = ev_lookup.get(key)
        if y_entry is None:
            continue
        ev_gbm = y_entry['ev_gbm']
        hv21   = y_entry['hv21']
        realized = float(row['realized_pnl_pct']) if row['realized_pnl_pct'] is not None else None
        unreal   = float(row['unrealized_pnl_pct']) if row['unrealized_pnl_pct'] is not None else None
        actual   = realized if realized is not None else unreal
        if actual is None:
            continue

        days_held = max(int(row['days_held'] or 1), 1)
        # GBM σ over the holding period. Clamp the vol to avoid the
        # degenerate case where a bad hv21 fetch → σ_holding ≈ 0 and
        # every signal trips the gate.
        sigma_holding = max(hv21 * _math.sqrt(days_held / TRADING_DAYS_PER_YEAR), SIGMA_FLOOR)
        delta = actual - ev_gbm
        sigma_delta = delta / sigma_holding if sigma_holding > 0 else 0.0

        if abs(sigma_delta) < SIGMA_GATE:
            continue

        # Trim to only fields the operator display + TradeJohn rules need.
        # Omit hv21 + sigma_holding (derivable from other columns) and entry/exit
        # (not used by the digest or the rules — present in yesterday's sized
        # handoff if needed).
        record = {
            'ticker':         row['ticker'],
            'strategy_id':    row['strategy_id'],
            'direction':      row['direction'],
            'status':         row['status'],
            'close_reason':   row['close_reason'],
            'realized_pct':   _safe(realized),
            'unrealized_pct': _safe(unreal),
            'ev_gbm':         _safe(ev_gbm),
            'delta':          _safe(delta),
            'sigma_delta':    _safe(sigma_delta, ndigits=2),
            'days_held':      days_held,
        }
        if sigma_delta >= SIGMA_GATE:
            overperformers.append(record)
        elif sigma_delta <= -SIGMA_GATE:
            underperformers.append(record)

    overperformers.sort(key=lambda r: (r.get('sigma_delta') or 0), reverse=True)
    underperformers.sort(key=lambda r: (r.get('sigma_delta') or 0))   # most negative first
    return overperformers, underperformers


def load_mastermind_rec(uri: str) -> dict | None:
    """Latest strategy_sizing_recommendations (derived from the Saturday
    comprehensive_review memos) — per-strategy sizing / stop / target /
    hold deltas that feed into TradeJohn's Monday handoff."""
    if not uri:
        return None
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT rec_date, strategy_id, current_size_pct, recommended_size_pct,
                   size_delta_pct, stop_delta_pct, target_delta_pct,
                   hold_days_delta, reasoning
              FROM strategy_sizing_recommendations
             WHERE rec_date = (SELECT MAX(rec_date) FROM strategy_sizing_recommendations)
               AND action_taken IN ('pending', 'applied')
            """
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return None
        recs = {r['strategy_id']: {
            'current_size_pct':     float(r['current_size_pct']) if r['current_size_pct'] is not None else None,
            'recommended_size_pct': float(r['recommended_size_pct']),
            'size_delta_pct':       float(r['size_delta_pct']) if r['size_delta_pct'] is not None else None,
            'stop_delta_pct':       float(r['stop_delta_pct']) if r['stop_delta_pct'] is not None else None,
            'target_delta_pct':     float(r['target_delta_pct']) if r['target_delta_pct'] is not None else None,
            'hold_days_delta':      int(r['hold_days_delta']) if r['hold_days_delta'] is not None else None,
            'reasoning':            r['reasoning'],
        } for r in rows}
        return {'run_date': str(rows[0]['rec_date']), 'recommendations': recs}
    except Exception:
        return None


# ── Per-signal features ─────────────────────────────────────────────────────

def _safe(x, ndigits: int | None = 4):
    """Return JSON-safe numeric: drop NaN/Inf; round floats to keep the
    handoff compact (400KB → ~100KB with ndigits=4, dramatically reducing
    TradeJohn prompt tokens)."""
    if x is None:
        return None
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        if ndigits is not None:
            return round(x, ndigits)
    return x


def compute_features(sig: dict, px: pd.DataFrame, spy_returns: pd.Series, run_date: str) -> dict | None:
    ticker = sig['ticker']
    entry  = sig.get('entry_price')
    stop   = sig.get('stop_loss')
    t1     = sig.get('target_1')
    t2     = sig.get('target_2') or (entry + 2 * (t1 - entry) if (entry and t1) else None)
    strat  = sig['strategy_id']
    params = sig.get('signal_params') or {}

    if ticker not in px.columns or entry is None or stop is None or t1 is None:
        return None
    ts = px[ticker].dropna()
    if len(ts) < 63:
        return None
    rets = ts.pct_change().dropna()

    # Vol
    hv21  = float(rets.tail(21).std() * math.sqrt(TRADING_DAYS_PER_YEAR))
    hv63  = float(rets.tail(63).std() * math.sqrt(TRADING_DAYS_PER_YEAR))
    hv252 = float(rets.std() * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(rets) >= 252 else hv63

    # Beta
    beta = None
    common = rets.index.intersection(spy_returns.index)
    if len(common) >= 60:
        rs = rets.loc[common].tail(252)
        rp = spy_returns.loc[common].tail(252)
        if len(rs) == len(rp) and rp.std() > 0:
            beta = float(np.cov(rs, rp)[0, 1] / rp.var())

    # Momentum
    def trailing(n):
        if len(ts) < n + 1:
            return None
        return float(ts.iloc[-1] / ts.iloc[-n] - 1)
    mom_1m  = trailing(21)
    mom_3m  = trailing(63)
    mom_6m  = trailing(126)
    mom_12m = params.get('lookback_ret') or params.get('momentum_12mo') or trailing(252)

    # RSI-14
    delta = rets.tail(15)
    gain  = delta.clip(lower=0).mean()
    loss  = (-delta.clip(upper=0)).mean()
    rsi   = float(100 - (100 / (1 + gain / loss))) if loss > 0 else 100.0

    # Risk / reward geometry
    risk_pts = entry - stop
    t1_pts   = t1 - entry
    t2_pts   = (t2 - entry) if t2 is not None else t1_pts * 2
    risk_pct = risk_pts / entry
    t1_pct   = t1_pts / entry
    t2_pct   = t2_pts / entry
    rr1      = t1_pts / max(risk_pts, 1e-6)

    # GBM two-barrier EV
    hp    = HOLDING_PERIOD.get(strat, DEFAULT_HP)
    mu_d  = (mom_12m / hp['target']) if (mom_12m is not None) else 0.0
    sig_d = hv21 / math.sqrt(TRADING_DAYS_PER_YEAR) if hv21 > 0 else 1e-6
    a     = math.log(stop / entry)   # negative
    b     = math.log(t1 / entry)     # positive
    mu_adj = mu_d - 0.5 * sig_d ** 2
    if sig_d > 0 and abs(mu_adj) < 1e-6:
        p_t1 = -a / (b - a)
    elif sig_d > 0:
        lam = 2 * mu_adj / (sig_d ** 2)
        try:
            p_t1 = (1 - math.exp(lam * a)) / (math.exp(lam * b) - math.exp(lam * a))
        except (OverflowError, ZeroDivisionError):
            p_t1 = 1.0 if mu_adj > 0 else 0.0
        p_t1 = max(0.0, min(1.0, p_t1))
    else:
        p_t1 = 0.5
    p_stop = 1.0 - p_t1
    ev_t1  = p_t1  * t1_pct  * 0.80   # 80% capture on reward
    ev     = ev_t1 + p_stop * (-risk_pct)

    # Expected exit
    if sig_d > 0:
        days_to_t1   = int(min(max((t1_pct / (mu_d + sig_d * 0.5)) if mu_d > 0 else hp['target'], hp['min']), hp['max']))
        days_to_stop = int(min(max((risk_pct / (sig_d * 1.5)), 5), hp['target']))
    else:
        days_to_t1 = hp['target']
        days_to_stop = hp['min']
    exp_exit_days = int(p_t1 * days_to_t1 + p_stop * days_to_stop)
    exp_exit_date = (datetime.strptime(run_date, '%Y-%m-%d').date()
                     + timedelta(days=int(exp_exit_days * 7 / 5))).isoformat()

    # Strategy-populated features live in signal_params.features (engine.py
    # folds Signal.features there). Merge over computed features so the
    # strategy's values win on key collisions — the strategy knows its own
    # domain (e.g. IV-RV ratio) better than our general-purpose computation.
    strategy_features = {}
    if isinstance(params, dict):
        raw = params.get('features')
        if isinstance(raw, dict):
            strategy_features = {k: _safe(v) for k, v in raw.items() if _safe(v) is not None}

    out = {
        'ticker':     ticker,
        'strategy_id':strat,
        'direction':  sig.get('direction') or 'long',
        'entry':      _safe(entry),
        'stop':       _safe(stop),
        't1':         _safe(t1),
        't2':         _safe(t2),
        'size_pct':   _safe(sig.get('position_size_pct')),
        'confidence': sig.get('confidence') or 'MED',
        'risk_pct':   _safe(risk_pct),
        't1_pct':     _safe(t1_pct),
        't2_pct':     _safe(t2_pct),
        'rr1':        _safe(rr1),
        'hv21':       _safe(hv21),
        'hv63':       _safe(hv63),
        'hv252':      _safe(hv252),
        'beta_spy':   _safe(beta),
        'rsi14':      _safe(rsi),
        'mom_1m':     _safe(mom_1m),
        'mom_3m':     _safe(mom_3m),
        'mom_6m':     _safe(mom_6m),
        'mom_12m':    _safe(mom_12m),
        'ev_gbm':     _safe(ev),
        'p_t1':       _safe(p_t1),
        'exp_exit_days': exp_exit_days,
        'exp_exit_date': exp_exit_date,
    }
    if strategy_features:
        out['strategy_features'] = strategy_features
    return out


# ── Main ───────────────────────────────────────────────────────────────────

def build(run_date: str) -> dict:
    uri = os.environ.get('POSTGRES_URI', '')
    signals = load_signals(uri, run_date) if uri else []
    print(f'[handoff] {len(signals)} signal(s) for {run_date}')

    regime     = load_regime(uri)
    portfolio  = load_portfolio_state()
    veto       = load_veto_history(uri)
    mm_rec     = load_mastermind_rec(uri)
    y_vetoed   = load_yesterdays_vetoed(run_date)
    y_overperf, y_underperf = load_yesterdays_performance_outliers(uri, run_date)
    print(f'[handoff] d-1 context: {len(y_vetoed)} vetoed / '
          f'{len(y_overperf)} overperformers / {len(y_underperf)} underperformers (|σΔ|≥1)')

    enriched: list[dict] = []
    if signals:
        try:
            px = load_prices()
            spy = None
            if 'SPY' in px.columns:
                spy = px['SPY'].pct_change().dropna()
        except Exception as e:
            print(f'[handoff] price load failed: {e}')
            px = pd.DataFrame()
            spy = pd.Series(dtype='float64')

        for sig in signals:
            try:
                feat = compute_features(sig, px, spy if spy is not None else pd.Series(dtype='float64'), run_date)
                if feat:
                    enriched.append(feat)
            except Exception as e:
                print(f'[handoff] {sig.get("ticker")}/{sig.get("strategy_id")}: {e}')

    # Pre-filter: split enriched into green-for-TradeJohn vs prefiltered.
    # The prefiltered list flows through to #trade-reports via send_report
    # so the operator still sees what got dropped and why.
    green: list[dict] = []
    prefiltered: list[dict] = []
    for f in enriched:
        ev = f.get('ev_gbm')
        p  = f.get('p_t1')
        if ev is None or ev < MIN_EV_GBM:
            prefiltered.append({
                'ticker':      f.get('ticker'),
                'strategy_id': f.get('strategy_id'),
                'direction':   f.get('direction'),
                'reason':      'prefilter_negative_ev' if (ev is not None and ev < 0) else 'prefilter_low_ev',
                'ev':          ev,
                'p_t1':        p,
            })
            continue
        if p is None or p < MIN_P_T1:
            prefiltered.append({
                'ticker':      f.get('ticker'),
                'strategy_id': f.get('strategy_id'),
                'direction':   f.get('direction'),
                'reason':      'prefilter_low_pt1',
                'ev':          ev,
                'p_t1':        p,
            })
            continue
        green.append(f)
    print(f'[handoff] prefilter: {len(green)} green / {len(prefiltered)} filtered '
          f'(min_ev={MIN_EV_GBM}, min_p_t1={MIN_P_T1})')

    payload = {
        'cycle_date':      run_date,
        'generated_at':    datetime.now(timezone.utc).isoformat(),
        'regime':          regime,
        'portfolio':       portfolio,
        'signals':         green,
        'prefiltered':     prefiltered,
        'veto_history_30d':veto,
        'yesterdays_vetoed':          y_vetoed,
        'yesterdays_overperformance': y_overperf,
        'yesterdays_underperformance':y_underperf,
        'mastermind_rec':  mm_rec,
        'stats': {
            'total_signals':       len(signals),
            'features_computed':   len(enriched),
            'green_signals':       len(green),
            'prefiltered':         len(prefiltered),
            'skipped_missing_data':len(signals) - len(enriched),
        },
    }
    write_handoff(run_date, 'structured', payload)
    print(f'[handoff] structured handoff written — {len(enriched)} signals, '
          f'{len(json.dumps(payload)) / 1024:.1f} KB')
    return payload


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    args = ap.parse_args()
    build(args.date)
