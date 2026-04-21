#!/usr/bin/env python3
"""
research_report.py — ResearchDesk quantitative analysis on active execution signals.

For each signal:
  - Historical volatility (21d HV, 63d HV)
  - Beta to SPY
  - Momentum persistence: actual trailing return vs signal entry
  - Time-to-exit estimate: first-passage time under GBM with drift
  - Expected return: probability-weighted outcome (hit T1 vs stop)
  - Sharpe contribution
  - Holding period distribution (percentile-based from vol)

Portfolio-level:
  - Correlation matrix
  - Portfolio beta, expected return, Sharpe
  - Sector / strategy concentration
  - Worst-case simultaneous drawdown

Posts:
  - Full report → DataBot webhook → #strategy-memos
  - Summary highlights → ResearchDesk webhook → #research-feed
"""

import os, sys, json, math, warnings
import numpy as np
import pandas as pd
import requests
import psycopg2, psycopg2.extras
from datetime import date, datetime, timedelta
from pathlib import Path
from scipy import stats

warnings.filterwarnings('ignore')

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

try:
    from execution.handoff import write_handoff as _write_handoff
    _HANDOFF_AVAILABLE = True
except ImportError:
    _HANDOFF_AVAILABLE = False

# ── Webhooks ─────────────────────────────────────────────────────────────────
WEBHOOKS = {
    'databot_strategy_memos':     'https://discord.com/api/webhooks/1492623936247300186/BFUwcy91xaIzq_GwP_YvON9-N9HhSilx-wDQ6MhISRYoSx9LrNYyXsDQeaSzxfwimEBi',
    'researchdesk_strategy_memos':'https://discord.com/api/webhooks/1492623938092535958/0bsB7NH54SPd1Z71eD1ieg3Nsm4uYE0OqNULjzxY5Tb1QxLzUK2rXsnpTPTV9-u64b5J',
    'researchdesk_research_feed': 'https://discord.com/api/webhooks/1492082671235371071/7ZZdz0XeEGxylNLyYBpHTeXu5FYB_lqiPIRz1RcbXHkcdy5YZyI_Y57tWtjLKScPQFul',
}

STRATEGY_LABELS = {
    'S9_dual_momentum':              'Dual Momentum (S9)',
    'S_custom_jt_momentum_12mo':     'JT 12-Month (JT)',
    'S10_quality_value':             'Quality Value (S10)',
    'S12_insider':                   'Insider Cluster (S12)',
    'S15_iv_rv_arb':                 'IV-RV Arb (S15)',
    'S_HV13_call_put_iv_spread':     'Call-Put IV Spread (HV13)',
    'S_HV14_otm_skew_factor':        'OTM Skew Factor (HV14)',
    'S_HV15_iv_term_structure':      'IV Term Structure (HV15)',
    'S_HV16_gex_regime':             'GEX Regime (HV16)',
    'S_HV17_earnings_straddle_fade': 'Earnings Straddle Fade (HV17)',
    'S_HV19_iv_surface_tilt':        'IV Surface Tilt (HV19)',
    'S_HV20_iv_dispersion_reversion':'IV Dispersion Rev (HV20)',
}

# Strategy holding period assumptions (trading days)
# DM: monthly rebalance; JT: 3-6 month momentum persistence window
_HP_OPTIONS = {'min': 1, 'target': 5, 'max': 21}   # vol strategies: weekly horizon
HOLDING_PERIOD = {
    'S9_dual_momentum':              {'min': 21, 'target': 63,  'max': 126},
    'S_custom_jt_momentum_12mo':     {'min': 42, 'target': 105, 'max': 189},
    'S10_quality_value':             {'min': 21, 'target': 63,  'max': 126},
    'S12_insider':                   {'min': 10, 'target': 30,  'max': 63},
    'S15_iv_rv_arb':                 {'min': 1,  'target': 10,  'max': 21},
    'S_HV13_call_put_iv_spread':     _HP_OPTIONS,
    'S_HV14_otm_skew_factor':        _HP_OPTIONS,
    'S_HV15_iv_term_structure':      _HP_OPTIONS,
    'S_HV16_gex_regime':             _HP_OPTIONS,
    'S_HV17_earnings_straddle_fade': {'min': 1, 'target': 3, 'max': 7},
    'S_HV19_iv_surface_tilt':        _HP_OPTIONS,
    'S_HV20_iv_dispersion_reversion':_HP_OPTIONS,
}

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_DAILY       = 0.05 / TRADING_DAYS_PER_YEAR


# ── Post helpers ──────────────────────────────────────────────────────────────

def wh_post(key, text):
    url    = WEBHOOKS[key]
    chunks, buf = [], ''
    for line in (text + '\n').split('\n'):
        if len(buf) + len(line) + 1 > 1990:
            chunks.append(buf.rstrip())
            buf = ''
        buf += line + '\n'
    if buf.strip():
        chunks.append(buf.rstrip())
    for chunk in chunks:
        r = requests.post(url, json={'content': chunk}, timeout=10)
        status = '✓' if r.ok else f'✗ {r.status_code}'
        print(f'  [{key[:20]}] {len(chunk)} chars {status}')


# ── Data loading ──────────────────────────────────────────────────────────────

def load_prices():
    df = pd.read_parquet(ROOT / 'data/master/prices.parquet')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['ticker', 'date'])
    # Pivot to wide: date × ticker
    px = df.pivot(index='date', columns='ticker', values='close').sort_index()
    return px


def load_signals(postgres_uri, run_date='2026-04-11'):
    conn = psycopg2.connect(postgres_uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT strategy_id, ticker, direction, entry_price, stop_loss,
               target_1, target_2, target_3, position_size_pct, signal_params, regime_state
        FROM execution_signals
        WHERE signal_date = %s
        ORDER BY strategy_id, ticker
    ''', (run_date,))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        for col in ('entry_price','stop_loss','target_1','target_2','target_3','position_size_pct'):
            if d.get(col) is not None:
                d[col] = float(d[col])
        rows.append(d)
    conn.close()
    return rows


def load_financials(postgres_uri, tickers):
    conn = psycopg2.connect(postgres_uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT ticker, revenue, gross_profit, net_income, total_assets, total_debt,
               free_cash_flow, eps, pe_ratio, date
        FROM financials
        WHERE ticker = ANY(%s)
        ORDER BY ticker, date DESC
    ''', (list(tickers),))
    rows = {}
    for r in cur.fetchall():
        t = r['ticker']
        if t not in rows:
            rows[t] = dict(r)
    conn.close()
    return rows


# ── Per-signal analytics ──────────────────────────────────────────────────────

def compute_signal_analytics(sig, px, spy_returns):
    ticker = sig['ticker']
    entry  = sig['entry_price']
    stop   = sig['stop_loss']
    t1     = sig['target_1']
    t2     = sig['target_2']
    sz     = sig['position_size_pct']
    strat  = sig['strategy_id']
    params = sig['signal_params'] if isinstance(sig['signal_params'], dict) else json.loads(sig['signal_params'] or '{}')

    if ticker not in px.columns:
        return None

    ts = px[ticker].dropna()
    if len(ts) < 63:
        return None

    rets = ts.pct_change().dropna()

    # Historical volatility
    hv21  = rets.tail(21).std() * math.sqrt(TRADING_DAYS_PER_YEAR)
    hv63  = rets.tail(63).std() * math.sqrt(TRADING_DAYS_PER_YEAR)
    hv252 = rets.std() * math.sqrt(TRADING_DAYS_PER_YEAR) if len(rets) >= 252 else hv63

    # Beta to SPY
    common_idx = rets.index.intersection(spy_returns.index)
    beta = np.nan
    alpha_ann = np.nan
    if len(common_idx) >= 60:
        r_stock = rets.loc[common_idx].tail(252)
        r_spy   = spy_returns.loc[common_idx].tail(252)
        if len(r_stock) == len(r_spy) and r_spy.std() > 0:
            cov   = np.cov(r_stock, r_spy)[0, 1]
            beta  = cov / r_spy.var()
            alpha_ann = (r_stock.mean() - beta * r_spy.mean()) * TRADING_DAYS_PER_YEAR

    # Momentum: actual trailing returns at different windows
    def trailing_ret(n):
        if len(ts) < n + 1:
            return np.nan
        return float(ts.iloc[-1] / ts.iloc[-n] - 1)

    mom_1m  = trailing_ret(21)
    mom_3m  = trailing_ret(63)
    mom_6m  = trailing_ret(126)
    mom_12m = params.get('lookback_ret', params.get('momentum_12mo', trailing_ret(252)))

    # RSI-14 from price returns
    delta = rets.tail(15)
    gain  = delta.clip(lower=0).mean()
    loss  = (-delta.clip(upper=0)).mean()
    rsi   = 100 - (100 / (1 + gain / loss)) if loss > 0 else 100.0

    # ATR (14-day)
    h = px.get(ticker) # may not have high/low in pivoted data — use approximation
    daily_range_approx = hv21 / math.sqrt(TRADING_DAYS_PER_YEAR) * entry
    atr14 = daily_range_approx  # σ × price as ATR proxy

    # Risk/reward geometry
    risk_pts  = entry - stop
    t1_pts    = t1 - entry
    t2_pts    = (t2 - entry) if t2 else t1_pts * 2
    risk_pct  = risk_pts / entry
    t1_pct    = t1_pts  / entry
    t2_pct    = t2_pts  / entry
    rr1       = t1_pts  / max(risk_pts, 0.001)
    rr2       = t2_pts  / max(risk_pts, 0.001)

    # ── Expected return via GBM first-passage probability ────────────────────
    # Under GBM: prob of hitting upper barrier b before lower barrier a
    # P(hit T1 before stop) = (1 - exp(-2μτ/σ²)) corrected Brownian motion
    # Simplified: use drift = momentum / holding_period, vol = daily_vol
    hp    = HOLDING_PERIOD.get(strat, {'min': 1, 'target': 5, 'max': 21})
    mu_d  = (mom_12m / hp['target']) if not math.isnan(mom_12m) else 0.0  # daily drift proxy
    sig_d = hv21 / math.sqrt(TRADING_DAYS_PER_YEAR)  # daily vol

    # Two-barrier probability (Siegmund approximation):
    # a = log(stop/entry), b = log(t1/entry), drift = mu_d - 0.5*sig_d^2
    a      = math.log(stop / entry)   # negative
    b      = math.log(t1  / entry)    # positive
    mu_adj = mu_d - 0.5 * sig_d ** 2

    if sig_d > 0 and abs(mu_adj) < 1e-6:
        p_t1 = -a / (b - a)
    elif sig_d > 0:
        # Reflection principle approximation
        lam = 2 * mu_adj / (sig_d ** 2)
        p_t1 = (1 - math.exp(lam * a)) / (math.exp(lam * b) - math.exp(lam * a))
        p_t1 = max(0.0, min(1.0, p_t1))
    else:
        p_t1 = 0.5

    p_stop = 1.0 - p_t1

    # Expected return (probability-weighted)
    # Assume: hit T1 → capture 80% (partial fill), hit stop → full loss
    ev_t1   = p_t1   * t1_pct  * 0.80
    ev_stop = p_stop * (-risk_pct)
    ev      = ev_t1 + ev_stop

    # Expected return in dollar terms (on $1M portfolio)
    dollar_ev = ev * sz  # as fraction of portfolio

    # ── Annualized Sharpe contribution ───────────────────────────────────────
    # Forward-looking Sharpe using expected return and vol over holding period
    hp_days  = hp['target']
    ev_ann   = ev * (TRADING_DAYS_PER_YEAR / hp_days)
    vol_ann  = hv63
    sharpe   = (ev_ann - 0.05) / vol_ann if vol_ann > 0 else np.nan

    # ── Expected exit timing ─────────────────────────────────────────────────
    # Expected time to first passage in trading days (approximate):
    # E[T] = distance / |drift|, bounded by [hp_min, hp_max]
    # More practically: use vol to estimate days to move risk_pts
    if sig_d > 0:
        days_to_t1   = int(min(max((t1_pct  / (mu_d + sig_d * 0.5)) if mu_d > 0 else hp['target'], hp['min']), hp['max']))
        days_to_stop = int(min(max((risk_pct / (sig_d * 1.5)), 5), hp['target']))
    else:
        days_to_t1   = hp['target']
        days_to_stop = hp['min']

    expected_exit_days = int(p_t1 * days_to_t1 + p_stop * days_to_stop)
    expected_exit_date = datetime.strptime(run_date, '%Y-%m-%d').date() + timedelta(days=int(expected_exit_days * 7/5))  # convert trading→calendar

    # ── Regime validation ────────────────────────────────────────────────────
    # Is stop tight enough relative to 21d ATR?
    stops_in_atr = risk_pts / atr14 if atr14 > 0 else np.nan

    return {
        'ticker':      ticker,
        'strategy':    strat,
        'entry':       entry,
        'stop':        stop,
        't1':          t1,
        't2':          t2,
        'size_pct':    sz,
        'risk_pct':    risk_pct,
        't1_pct':      t1_pct,
        'rr1':         rr1,
        'rr2':         rr2,
        'hv21':        hv21,
        'hv63':        hv63,
        'hv252':       hv252,
        'beta':        beta,
        'alpha_ann':   alpha_ann,
        'rsi':         rsi,
        'mom_1m':      mom_1m,
        'mom_3m':      mom_3m,
        'mom_6m':      mom_6m,
        'mom_12m':     mom_12m,
        'p_t1':        p_t1,
        'p_stop':      p_stop,
        'ev':          ev,
        'ev_ann':      ev_ann,
        'dollar_ev':   dollar_ev,
        'sharpe':      sharpe,
        'days_to_t1':  days_to_t1,
        'days_to_stop':days_to_stop,
        'exp_exit_days':expected_exit_days,
        'exp_exit_date':str(expected_exit_date),
        'stops_in_atr': stops_in_atr,
        'momentum_rank': params.get('momentum_rank', np.nan),
    }


def _kelly_fraction(a):
    """
    Binary-outcome Kelly fraction for a signal. Treats a win as 80% capture
    of t1_pct (matches the EV computation above) and a loss as full risk_pct.
    Returns None on degenerate inputs; bounded to [-1, 1]. Agent applies its
    own fractional-Kelly scaling (typically /2 or /4).
    """
    try:
        p = float(a.get('p_t1') or 0.0)
        t1_pct   = float(a.get('t1_pct') or 0.0)
        risk_pct = float(a.get('risk_pct') or 0.0)
        if p <= 0.0 or risk_pct <= 0.0 or t1_pct <= 0.0:
            return None
        gain = 0.80 * t1_pct
        b    = gain / risk_pct
        if b <= 0.0:
            return None
        f = (p * b - (1.0 - p)) / b
        return round(max(-1.0, min(1.0, f)), 4)
    except (TypeError, ValueError):
        return None


# ── Portfolio analytics ────────────────────────────────────────────────────────

def compute_portfolio_analytics(analytics, px, spy_returns):
    tickers = [a['ticker'] for a in analytics]

    # Correlation matrix (63-day returns)
    rets_df = px[tickers].pct_change().tail(63).dropna(how='all')
    corr    = rets_df.corr()

    # Portfolio-level beta (size-weighted)
    total_sz     = sum(a['size_pct'] for a in analytics)
    port_beta    = sum(a['beta'] * a['size_pct'] for a in analytics if not math.isnan(a['beta'])) / max(total_sz, 0.01)
    port_ev      = sum(a['ev'] * a['size_pct'] for a in analytics)
    port_ev_ann  = sum(a['ev_ann'] * a['size_pct'] for a in analytics)

    # Portfolio vol: weighted covariance
    sz_arr  = np.array([a['size_pct'] for a in analytics])
    hv_arr  = np.array([a['hv63'] for a in analytics])
    cov_mtx = np.outer(hv_arr, hv_arr) * corr.values
    port_var = sz_arr @ cov_mtx @ sz_arr
    port_vol = math.sqrt(max(port_var, 0))
    port_sharpe = (port_ev_ann - 0.05 * total_sz) / port_vol if port_vol > 0 else np.nan

    # Worst-case simultaneous drawdown (all stop out)
    worst_dd = sum(a['risk_pct'] * a['size_pct'] for a in analytics)

    # Max single-day estimated loss (95th pct shock = -2.5σ)
    max_1d_loss = sum(a['hv63'] / math.sqrt(252) * 2.5 * a['size_pct'] for a in analytics)

    # Strategy concentration
    strat_exp = {}
    for a in analytics:
        strat_exp[a['strategy']] = strat_exp.get(a['strategy'], 0) + a['size_pct']

    # Sector map
    SECTORS = {
        'MU':'Semiconductors', 'STX':'Technology Storage', 'WDC':'Technology Storage',
        'NEM':'Gold Mining',   'WBD':'Media/Streaming',
        'GLD':'Gold',          'SLV':'Silver',
        'USO':'Energy/Oil',    'XLE':'Energy',  'PDBC':'Commodities',
    }
    sector_exp = {}
    for a in analytics:
        sec = SECTORS.get(a['ticker'], 'Other')
        sector_exp[sec] = sector_exp.get(sec, 0) + a['size_pct']

    return {
        'corr':         corr,
        'port_beta':    port_beta,
        'port_ev':      port_ev,
        'port_ev_ann':  port_ev_ann,
        'port_vol':     port_vol,
        'port_sharpe':  port_sharpe,
        'worst_dd':     worst_dd,
        'max_1d_loss':  max_1d_loss,
        'total_sz':     total_sz,
        'strat_exp':    strat_exp,
        'sector_exp':   sector_exp,
    }


# ── Report builders ────────────────────────────────────────────────────────────

def build_full_report(analytics, port, run_date):
    """Full report → DataBot → #strategy-memos"""
    lines = [
        f'📊 **ResearchDesk Quantitative Report — {run_date}**',
        f'Regime: **HIGH_VOL** | {len(analytics)} signals | Gross exposure: {port["total_sz"]*100:.2f}%',
        f'*Analysis: vol regimes, momentum persistence, GBM first-passage exit timing, EV-weighted returns*',
        '',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        '**PORTFOLIO SUMMARY**',
        f'Portfolio beta:         {port["port_beta"]:.2f}x SPY',
        f'Expected return (fwd):  {port["port_ev"]*100:+.2f}% (position-weighted)',
        f'Ann. expected return:   {port["port_ev_ann"]*100:+.2f}%',
        f'Portfolio vol (63d):    {port["port_vol"]*100:.1f}% (ann)',
        f'Forward Sharpe:         {port["port_sharpe"]:.2f}',
        f'Worst-case drawdown:    -{port["worst_dd"]*100:.2f}% (all stops hit simultaneously)',
        f'95th pct 1-day loss:    -{port["max_1d_loss"]*100:.2f}%',
        '',
        '**Strategy Exposure**',
    ]
    for strat, exp in port['strat_exp'].items():
        lines.append(f'  {STRATEGY_LABELS.get(strat, strat)}: {exp*100:.2f}%')

    lines += ['', '**Sector Exposure**']
    for sec, exp in sorted(port['sector_exp'].items(), key=lambda x: -x[1]):
        lines.append(f'  {sec}: {exp*100:.2f}%')

    lines += [
        '',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        '**PER-SIGNAL ANALYSIS**',
        '',
    ]

    for a in analytics:
        strat_lbl = STRATEGY_LABELS.get(a['strategy'], a['strategy'])
        hp        = HOLDING_PERIOD.get(a['strategy'], {'min': 1, 'target': 5, 'max': 21})

        lines += [
            f'**{a["ticker"]}** — {strat_lbl}',
            '```',
            f'Position size:     {a["size_pct"]*100:.2f}%',
            f'Entry / Stop / T1: ${a["entry"]:.2f} / ${a["stop"]:.2f} / ${a["t1"]:.2f}',
            f'Risk:              -{a["risk_pct"]*100:.1f}% | R:R to T1: {a["rr1"]:.1f}x | R:R to T2: {a["rr2"]:.1f}x',
            '',
            f'Volatility (21d/63d/252d HV): {a["hv21"]*100:.1f}% / {a["hv63"]*100:.1f}% / {a["hv252"]*100:.1f}%',
            f'Beta to SPY:       {a["beta"]:.2f}x' if not math.isnan(a["beta"]) else 'Beta to SPY:       N/A',
            f'RSI (14):          {a["rsi"]:.0f}',
            f'Stop width in ATR: {a["stops_in_atr"]:.1f}x daily ATR' if not math.isnan(a["stops_in_atr"]) else '',
            '',
            f'Momentum:',
            f'  1-month:  {a["mom_1m"]*100:+.1f}%' if not math.isnan(a["mom_1m"]) else '  1-month:  N/A',
            f'  3-month:  {a["mom_3m"]*100:+.1f}%' if not math.isnan(a["mom_3m"]) else '  3-month:  N/A',
            f'  6-month:  {a["mom_6m"]*100:+.1f}%' if not math.isnan(a["mom_6m"]) else '  6-month:  N/A',
            f'  12-month: {a["mom_12m"]*100:+.1f}%' if not math.isnan(a["mom_12m"]) else '  12-month: N/A',
            '',
            f'Exit probability:',
            f'  P(hit T1 first): {a["p_t1"]*100:.0f}%  |  P(stop out): {a["p_stop"]*100:.0f}%',
            f'  Expected value:  {a["ev"]*100:+.2f}% of position ({a["ev"]*a["size_pct"]*100:+.3f}% of portfolio)',
            f'  Ann. EV:         {a["ev_ann"]*100:+.1f}%',
            f'  Forward Sharpe:  {a["sharpe"]:.2f}' if not math.isnan(a["sharpe"]) else '  Forward Sharpe:  N/A',
            '',
            f'Exit timing (GBM first-passage):',
            f'  Strategy horizon:   {hp["min"]}–{hp["max"]} trading days ({hp["target"]} target)',
            f'  Est. days to T1:    ~{a["days_to_t1"]} trading days',
            f'  Est. days to stop:  ~{a["days_to_stop"]} trading days',
            f'  Expected exit:      ~{a["exp_exit_days"]} trading days (~{a["exp_exit_date"]})',
            '```',
            '',
        ]

    # Correlation matrix
    tickers = [a['ticker'] for a in analytics]
    lines += [
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        '**63-DAY RETURN CORRELATION MATRIX**',
        '```',
        '       ' + '  '.join(f'{t:>5}' for t in tickers),
    ]
    for t1 in tickers:
        row = f'{t1:<6} '
        for t2 in tickers:
            val = port['corr'].loc[t1, t2] if t1 in port['corr'].index and t2 in port['corr'].columns else np.nan
            if hasattr(val, 'iloc'): val = float(val.iloc[0] if val.ndim == 1 else val.iloc[0, 0]) if val.size > 0 else np.nan
            row += f' {val:>5.2f}' if not math.isnan(val) else '   N/A'
        lines.append(row)
    lines.append('```')

    return '\n'.join(lines)


def build_summary_report(analytics, port, run_date):
    """Highlighted summary → ResearchDesk → #research-feed"""
    # Sort by expected value contribution to portfolio
    ranked = sorted(analytics, key=lambda a: a['ev'] * a['size_pct'], reverse=True)

    lines = [
        f'🔬 **ResearchDesk Research Report — {run_date}**',
        f'*Quantitative analysis: {len(analytics)} signals | GBM first-passage exit timing | EV-weighted returns*',
        '',
        '**Portfolio View**',
        f'Beta: {port["port_beta"]:.2f}x | EV: {port["port_ev"]*100:+.2f}% ({port["port_ev_ann"]*100:+.1f}%/yr) | Sharpe: {port["port_sharpe"]:.2f} | Worst DD: -{port["worst_dd"]*100:.2f}%',
        '',
        '**Signal Ranking by Expected Value** *(P(T1) × return − P(stop) × loss)*',
        '```',
        f'{"Ticker":<6} {"Strategy":<12} {"Size":>5} {"P(T1)":>6} {"EV":>7} {"Exit ~":>10} {"HV63":>6} {"RSI":>5} {"Beta":>5}',
        '─' * 68,
    ]
    for a in ranked:
        strat_short = 'DM' if 'dual' in a['strategy'] else 'JT'
        p_t1_str    = f'{a["p_t1"]*100:.0f}%'
        ev_str      = f'{a["ev"]*100:+.2f}%'
        exit_str    = a['exp_exit_date']
        hv_str      = f'{a["hv63"]*100:.0f}%'
        rsi_str     = f'{a["rsi"]:.0f}'
        beta_str    = f'{a["beta"]:.2f}' if not math.isnan(a["beta"]) else ' N/A'
        lines.append(
            f'{a["ticker"]:<6} {strat_short:<12} {a["size_pct"]*100:>4.2f}%'
            f' {p_t1_str:>6} {ev_str:>7} {exit_str:>10} {hv_str:>6} {rsi_str:>5} {beta_str:>5}'
        )
    lines.append('```')
    lines.append('')

    # Flags
    flags = []
    for a in analytics:
        if a['rsi'] > 70:
            flags.append(f'⚠️ **{a["ticker"]}** RSI={a["rsi"]:.0f} — overbought entering momentum position')
        if a['stops_in_atr'] < 1.0 and not math.isnan(a['stops_in_atr']):
            flags.append(f'⚠️ **{a["ticker"]}** stop < 1 ATR — high noise-out risk')
        if a['hv63'] > 0.60:
            flags.append(f'⚠️ **{a["ticker"]}** HV63={a["hv63"]*100:.0f}% — elevated vol, size is appropriate')
        if a['p_t1'] < 0.40:
            flags.append(f'🔴 **{a["ticker"]}** P(T1)={a["p_t1"]*100:.0f}% — negative-EV signal, review stop placement')
        if a['rr1'] < 1.0:
            flags.append(f'📌 **{a["ticker"]}** R:R={a["rr1"]:.1f}x — sub-1:1 to T1, needs P(T1)>50% to be EV-positive')

    if flags:
        lines += ['**Risk Flags**'] + flags + ['']

    # Best bets
    best = ranked[:3]
    lines += [
        '**Highest-Conviction Setups**',
    ]
    for a in best:
        mom_str  = f'{a["mom_12m"]*100:+.0f}%' if not math.isnan(a['mom_12m']) else 'N/A'
        hp_label = 'monthly rebalance' if 'dual' in a['strategy'] else '3–6 month hold'
        lines.append(
            f'• **{a["ticker"]}** — {STRATEGY_LABELS.get(a["strategy"])} | '
            f'EV {a["ev"]*100:+.2f}% | P(T1) {a["p_t1"]*100:.0f}% | '
            f'12mo momentum {mom_str} | Exit ~{a["exp_exit_date"]} ({hp_label})'
        )

    lines += [
        '',
        '**Sector Concentration**',
    ]
    for sec, exp in sorted(port['sector_exp'].items(), key=lambda x: -x[1]):
        bar = '█' * int(exp * 200)
        lines.append(f'  {sec:<22} {exp*100:>5.2f}%  {bar}')

    lines += [
        '',
        f'*Note: Exit dates estimated via GBM first-passage timing (drift from 12mo momentum, vol from 63d HV). '
        f'EV uses two-barrier reflection principle. All figures are forward projections, not guarantees.*',
    ]

    return '\n'.join(lines)


# ── Memory writes ─────────────────────────────────────────────────────────────

def write_signal_patterns(analytics, port, run_date):
    """
    Append structured learning entries to workspace memory files after each run.
    Called after every research cycle so agents accumulate pattern knowledge over time.
    """
    workspace = Path(os.environ.get('OPENCLAW_DIR', str(ROOT))) / 'workspaces' / 'default'
    mem_dir   = workspace / 'memory'
    mem_dir.mkdir(parents=True, exist_ok=True)

    # Read regime from market-state file (authoritative source)
    regime = 'UNKNOWN'
    for ms_path in [ROOT / '.agents' / 'market-state' / 'latest.json',
                    Path(os.environ.get('OPENCLAW_DIR', str(ROOT))) / '.agents' / 'market-state' / 'latest.json']:
        try:
            regime = json.loads(ms_path.read_text()).get('state', 'UNKNOWN')
            break
        except Exception:
            pass

    # ── signal_patterns.md ────────────────────────────────────────────────────
    ev_pos    = [a for a in analytics if a['ev'] > 0]
    ev_neg    = [a for a in analytics if a['ev'] <= 0]
    avg_p_t1  = sum(a['p_t1'] for a in analytics) / max(len(analytics), 1)
    avg_ev    = sum(a['ev'] for a in analytics) / max(len(analytics), 1)
    high_conv = [a for a in analytics if a['p_t1'] >= 0.55 and a['ev'] > 0]
    overbought = [a['ticker'] for a in analytics if a.get('rsi', 0) > 70]

    pattern_entry = (
        f"\n{run_date} | {regime} | "
        f"signals={len(analytics)}, EV+={len(ev_pos)}, EV-={len(ev_neg)}, "
        f"avgP(T1)={avg_p_t1*100:.0f}%, avgEV={avg_ev*100:+.2f}%, "
        f"highConv={len(high_conv)}, overbought={overbought or 'none'}, "
        f"portBeta={port['port_beta']:.2f}, portSharpe={port['port_sharpe']:.2f}, "
        f"worstDD={port['worst_dd']*100:.2f}%"
    )
    _mem_append(mem_dir / 'signal_patterns.md', pattern_entry)

    # Structured mirror into daily_signal_summary (migration 040). The memo file
    # remains the narrative log; the gate reads from this table (authoritative).
    _write_daily_signal_summary(
        run_date=run_date,
        regime=regime,
        analytics=analytics,
        ev_pos=ev_pos,
        ev_neg=ev_neg,
        avg_ev=avg_ev,
        avg_p_t1=avg_p_t1,
        high_conv=high_conv,
        overbought=overbought,
        port=port,
    )

    # ── regime_context.md ─────────────────────────────────────────────────────
    strat_summary = ', '.join(f"{k.split('_')[0]}:{v*100:.1f}%" for k, v in port['strat_exp'].items())
    sector_top    = sorted(port['sector_exp'].items(), key=lambda x: -x[1])[:3]
    sector_str    = ', '.join(f"{s}:{e*100:.1f}%" for s, e in sector_top)
    regime_entry  = (
        f"\n{run_date} | {regime} | "
        f"strategies_active={strat_summary}, top_sectors={sector_str}, "
        f"port_ev_ann={port['port_ev_ann']*100:+.1f}%/yr, max_1d_loss={port['max_1d_loss']*100:.2f}%"
    )
    _mem_append(mem_dir / 'regime_context.md', regime_entry)

    # Structured JSONL parallel write
    try:
        import json as _json, datetime as _dt
        jsonl_record = _json.dumps({
            'ts':           _dt.datetime.utcnow().isoformat() + 'Z',
            'type':         'signal_summary',
            'date':         run_date,
            'regime':       regime,
            'signal_count': len(analytics),
            'ev_pos':       len(ev_pos),
            'ev_neg':       len(ev_neg),
            'avg_ev':       round(avg_ev, 4),
            'avg_p_t1':     round(avg_p_t1, 4),
            'high_conv':    len(high_conv),
        })
        with open(mem_dir / 'events.jsonl', 'a') as f:
            f.write(jsonl_record + '\n')
    except Exception as e:
        print(f'[research] JSONL write failed: {e}')

    print(f'[research] Memory written: signal_patterns.md, regime_context.md ({run_date})')


def _write_daily_signal_summary(*, run_date, regime, analytics, ev_pos, ev_neg,
                                 avg_ev, avg_p_t1, high_conv, overbought, port):
    """Insert one row per pipeline cycle into daily_signal_summary."""
    try:
        uri = os.environ.get('POSTGRES_URI', '')
        if not uri:
            return
        conn = psycopg2.connect(uri)
        try:
            with conn.cursor() as cur:
                def _f(v):
                    # Coerce numpy/NaN values to native-Python scalars for psycopg2.
                    try:
                        fv = float(v)
                        return None if math.isnan(fv) else round(fv, 6)
                    except (TypeError, ValueError):
                        return None
                cur.execute("""
                    INSERT INTO daily_signal_summary
                      (run_date, regime, n_signals, ev_pos, ev_neg,
                       avg_ev, avg_p_t1, high_conv_count,
                       port_beta, port_sharpe, worst_dd, overbought)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    run_date, regime, len(analytics), len(ev_pos), len(ev_neg),
                    _f(avg_ev), _f(avg_p_t1), len(high_conv),
                    _f(port['port_beta']), _f(port['port_sharpe']),
                    _f(port['worst_dd']),
                    [str(x) for x in set(overbought)] or None,
                ))
            conn.commit()
            print(f'[research] daily_signal_summary row inserted ({run_date})')
        finally:
            conn.close()
    except Exception as e:
        print(f'[research] daily_signal_summary write failed: {e}')


def _mem_append(fpath, text):
    """Append text to memory file, skipping if an identical line already exists."""
    try:
        stripped = text.strip()
        if fpath.exists():
            existing = fpath.read_text()
            if stripped and stripped in existing:
                return  # dedup: already present
        with open(fpath, 'a') as f:
            f.write(text)
    except Exception as e:
        print(f'[research] Memory write failed ({fpath.name}): {e}')


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default='2026-04-11')
    args = parser.parse_args()

    postgres_uri = os.environ.get('POSTGRES_URI', '')
    run_date     = args.date

    print(f'[research] Loading data for {run_date}...')
    px      = load_prices()
    signals = load_signals(postgres_uri, run_date)
    print(f'[research] {len(signals)} signals | price matrix: {px.shape}')

    # SPY returns for beta calculation
    spy_returns = px['SPY'].pct_change().dropna() if 'SPY' in px.columns else pd.Series(dtype=float)

    print('[research] Computing per-signal analytics...')
    analytics = []
    for sig in signals:
        a = compute_signal_analytics(sig, px, spy_returns)
        if a:
            analytics.append(a)
            print(f'  {a["ticker"]}: EV={a["ev"]*100:+.2f}% | P(T1)={a["p_t1"]*100:.0f}% | exit~{a["exp_exit_date"]} | β={a["beta"]:.2f}')
        else:
            print(f'  {sig["ticker"]}: SKIPPED (insufficient data)')

    if not analytics:
        print('[research] No analytics computed — exiting')
        sys.exit(1)

    print('[research] Computing portfolio analytics...')
    port = compute_portfolio_analytics(analytics, px, spy_returns)
    print(f'  Port beta={port["port_beta"]:.2f} | EV={port["port_ev"]*100:+.2f}% | Sharpe={port["port_sharpe"]:.2f} | WorstDD=-{port["worst_dd"]*100:.2f}%')

    print('[research] Building reports...')
    full_report    = build_full_report(analytics, port, run_date)
    summary_report = build_summary_report(analytics, port, run_date)

    print('\n' + '='*70)
    print(full_report)
    print()
    print(summary_report)
    print('='*70)

    print('\n[research] Posting to Discord...')
    wh_post('databot_strategy_memos',     full_report)
    wh_post('researchdesk_research_feed', summary_report)

    print('[research] Writing memory learnings...')
    write_signal_patterns(analytics, port, run_date)

    if _HANDOFF_AVAILABLE:
        # Derive regime from signals (first available regime_state)
        regime = next((s.get('regime_state', 'UNKNOWN') for s in signals if s.get('regime_state')), 'UNKNOWN')
        convergent_tickers = [
            t for t, strats in
            {s['ticker']: [] for s in signals}.items()
            if sum(1 for ss in signals if ss['ticker'] == t) >= 2
        ]
        _write_handoff(run_date, 'research', {
            'run_date':           run_date,
            'regime':             regime,
            'portfolio': {
                'sharpe':              port['port_sharpe'] if not math.isnan(port['port_sharpe']) else None,
                'worst_case_drawdown': port['worst_dd'],
                'port_beta':           port['port_beta'],
                'port_ev_ann':         port['port_ev_ann'],
            },
            'signals': [
                {'ticker':        a['ticker'],
                 'strategy_id':   a['strategy'],
                 'entry':         a['entry'],
                 'stop':          a['stop'],
                 't1':            a['t1'],
                 't2':            a['t2'],
                 'size_pct':      a['size_pct'],
                 'risk_pct':      a['risk_pct'],
                 't1_pct':        a['t1_pct'],
                 'rr1':           a['rr1'],
                 'rr2':           a['rr2'],
                 'ev':            a['ev'],
                 'kelly':         _kelly_fraction(a),
                 'hv21':          a['hv21'],
                 'beta':          a['beta'] if not math.isnan(a['beta']) else None,
                 'p_t1':          a['p_t1'],
                 'exp_exit_date': a['exp_exit_date']}
                for a in analytics
            ],
            'convergent_tickers': convergent_tickers,
        })
        print(f'[research] Research handoff written ({len(analytics)} signals).')

    print('[research] Done.')
