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


def load_ev_calibration(uri: str, window_days: int = 30) -> dict:
    """Per-strategy rolling calibration stats from signal_pnl (closed trades).

    Returns one entry per strategy that has ≥ 1 closed trade in the window.
    Uses observables only (no predicted EV reconstruction). Downstream
    TradeJohn consumes this via `fundjohn:ev-calibrator`.
    """
    if not uri:
        return {}
    half = max(1, window_days // 2)
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            WITH closed AS (
              SELECT strategy_id,
                     realized_pnl_pct::float AS pnl,
                     closed_at
                FROM signal_pnl
               WHERE status = 'closed'
                 AND realized_pnl_pct IS NOT NULL
                 AND closed_at >= (CURRENT_DATE - (%s || ' days')::interval)
            ),
            recent AS (
              SELECT strategy_id, AVG(pnl) AS avg_pnl
                FROM closed
               WHERE closed_at >= (CURRENT_DATE - (%s || ' days')::interval)
               GROUP BY strategy_id
            ),
            prior AS (
              SELECT strategy_id, AVG(pnl) AS avg_pnl
                FROM closed
               WHERE closed_at <  (CURRENT_DATE - (%s || ' days')::interval)
               GROUP BY strategy_id
            ),
            overall AS (
              SELECT strategy_id,
                     COUNT(*)                                   AS n_closed,
                     AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)   AS hit_rate,
                     AVG(pnl)                                   AS avg_pnl,
                     STDDEV_SAMP(pnl)                           AS std_pnl
                FROM closed
               GROUP BY strategy_id
            )
            SELECT o.strategy_id,
                   o.n_closed,
                   o.hit_rate,
                   o.avg_pnl,
                   o.std_pnl,
                   r.avg_pnl AS recent_pnl_avg,
                   p.avg_pnl AS prior_pnl_avg
              FROM overall o
              LEFT JOIN recent r USING (strategy_id)
              LEFT JOIN prior  p USING (strategy_id)
             ORDER BY o.strategy_id
            """,
            (window_days, half, half),
        )
        out: dict[str, dict] = {}
        today_iso = date.today().isoformat()
        for row in cur.fetchall():
            avg  = float(row['avg_pnl'] or 0.0)
            std  = float(row['std_pnl'] or 0.0)
            rec  = row['recent_pnl_avg']
            pri  = row['prior_pnl_avg']
            recent_avg = float(rec) if rec is not None else None
            prior_avg  = float(pri) if pri is not None else None
            drift = None
            if recent_avg is not None and prior_avg is not None:
                drift = recent_avg - prior_avg
            sharpe_proxy = (avg / std) if std > 0 else None
            out[row['strategy_id']] = {
                'n_closed':            int(row['n_closed'] or 0),
                'hit_rate':            _safe(float(row['hit_rate'] or 0.0)),
                'realized_pnl_avg':    _safe(avg),
                'realized_pnl_stdev':  _safe(std),
                'sharpe_proxy':        _safe(sharpe_proxy),
                'recent_pnl_avg':      _safe(recent_avg),
                'prior_pnl_avg':       _safe(prior_avg),
                'drift_score':         _safe(drift),
                'window_days':         window_days,
                'last_updated':        today_iso,
            }
        conn.close()
        return out
    except Exception as e:
        print(f'[handoff] ev_calibration unavailable: {e}')
        return {}


def load_open_positions(uri: str) -> list[dict]:
    """Currently open positions from execution_signals (status='open')."""
    if not uri:
        return []
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT strategy_id, ticker, direction, position_size_pct
              FROM execution_signals
             WHERE status = 'open' AND ticker IS NOT NULL
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        for r in rows:
            v = r.get('position_size_pct')
            r['position_size_pct'] = float(v) if v is not None else None
        return rows
    except Exception as e:
        print(f'[handoff] open positions unavailable: {e}')
        return []


def compute_correlation_matrix(
    tickers: list[str],
    px: pd.DataFrame,
    window_days: int = 60,
    cap: int = 30,
) -> dict | None:
    """Pairwise correlation of daily returns over the last `window_days`.

    Capped at `cap` tickers (largest coverage first) to bound handoff size
    — a 30×30 matrix of 2-decimal floats is ~4 KB, versus 30 KB for 60×60.
    """
    tickers = [t for t in dict.fromkeys(tickers) if t in px.columns]
    if len(tickers) < 2:
        return None
    # Prefer tickers with full coverage in the window
    window = px.tail(window_days + 1)
    coverage = window[tickers].notna().sum().sort_values(ascending=False)
    tickers = coverage.head(cap).index.tolist()
    if len(tickers) < 2:
        return None
    rets = window[tickers].pct_change().dropna(how='all')
    if len(rets) < 10:
        return None
    corr = rets.corr().fillna(0.0)
    # Round aggressively — matrix is for eyeballing, not linear algebra.
    return {
        'as_of':        str(px.index[-1].date()) if hasattr(px.index[-1], 'date') else None,
        'window_days':  window_days,
        'tickers':      tickers,
        'matrix':       [[round(float(corr.iat[i, j]), 2) for j in range(len(tickers))]
                         for i in range(len(tickers))],
    }


def load_mastermind_rec(uri: str) -> dict | None:
    """Latest strategy-stack recommendation from MastermindJohn weekly."""
    if not uri:
        return None
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT run_date, recommendations
              FROM mastermind_weekly_reports
             WHERE mode = 'strategy-stack'
             ORDER BY run_date DESC LIMIT 1
            """
        )
        r = cur.fetchone()
        conn.close()
        if r and r.get('recommendations'):
            recs = r['recommendations']
            if isinstance(recs, str):
                recs = json.loads(recs)
            return {'run_date': str(r['run_date']), 'recommendations': recs}
    except Exception:
        pass
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

    regime        = load_regime(uri)
    portfolio     = load_portfolio_state()
    veto          = load_veto_history(uri)
    mm_rec        = load_mastermind_rec(uri)
    ev_calib      = load_ev_calibration(uri)
    open_pos      = load_open_positions(uri)

    enriched: list[dict] = []
    px = pd.DataFrame()
    spy = pd.Series(dtype='float64')
    if signals or open_pos:
        try:
            px = load_prices()
            if 'SPY' in px.columns:
                spy = px['SPY'].pct_change().dropna()
        except Exception as e:
            print(f'[handoff] price load failed: {e}')

        for sig in signals:
            try:
                feat = compute_features(sig, px, spy, run_date)
                if feat:
                    enriched.append(feat)
            except Exception as e:
                print(f'[handoff] {sig.get("ticker")}/{sig.get("strategy_id")}: {e}')

    # Correlation matrix over current open positions + today's enriched
    # ticker set. Deterministic, ≤30 tickers, compact (~4 KB).
    corr_tickers: list[str] = []
    strategy_map: dict[str, str] = {}
    for p in open_pos:
        t = p.get('ticker')
        if t and t not in strategy_map:
            strategy_map[t] = p.get('strategy_id') or ''
            corr_tickers.append(t)
    for s in enriched:
        t = s.get('ticker')
        if t and t not in strategy_map:
            strategy_map[t] = s.get('strategy_id') or ''
            corr_tickers.append(t)
    correlation_matrix = None
    if not px.empty and corr_tickers:
        try:
            correlation_matrix = compute_correlation_matrix(corr_tickers, px)
            if correlation_matrix is not None:
                # Keep strategy_map aligned to the (possibly filtered) ticker list
                kept = set(correlation_matrix['tickers'])
                correlation_matrix['strategy_map'] = {
                    t: strategy_map[t] for t in kept if t in strategy_map
                }
        except Exception as e:
            print(f'[handoff] correlation_matrix failed: {e}')

    payload = {
        'cycle_date':         run_date,
        'generated_at':       datetime.now(timezone.utc).isoformat(),
        'regime':             regime,
        'portfolio':          portfolio,
        'signals':            enriched,
        'veto_history_30d':   veto,
        'mastermind_rec':     mm_rec,
        'ev_calibration':     ev_calib,
        'correlation_matrix': correlation_matrix,
        'stats': {
            'total_signals':         len(signals),
            'features_computed':     len(enriched),
            'skipped_missing_data':  len(signals) - len(enriched),
            'ev_calib_strategies':   len(ev_calib),
            'correlation_tickers':   len(correlation_matrix['tickers']) if correlation_matrix else 0,
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
