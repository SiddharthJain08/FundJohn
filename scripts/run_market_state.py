#!/usr/bin/env python3
"""
MARKET_STATE Runner — executes data-prep MARKET_STATE mode against live data.
Run from /root/openclaw: python3 scripts/run_market_state.py

Uses real tool function signatures (not the prompt-level pseudocode).
yfinance used directly for VIX/ETF series not available via polygon tool.
"""

import sys, os, json, pickle, warnings, time
import pandas as pd
import numpy as np
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')

# Add workspace tools to path
TOOLS_DIR = Path('workspaces/default/tools')
sys.path.insert(0, str(TOOLS_DIR))

# Load env
from dotenv import load_dotenv
load_dotenv('.env')

TODAY     = date.today().isoformat()
LOOKBACK  = 252
MODEL_DIR = Path('.agents/market-state')
MODEL_DIR.mkdir(parents=True, exist_ok=True)

print(f'\n[market-state] Starting MARKET_STATE run — {TODAY}')

# ── Step 1: Feature Data ──────────────────────────────────────────────────────

print('[market-state] Step 1 — Fetching feature data...')

import yfinance as yf

def yf_close(symbol, period='1y'):
    df = yf.download(symbol, period=period, interval='1d', auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f'No data for {symbol}')
    # Flatten MultiIndex columns (yfinance >= 0.2 returns ('Close', 'TICKER'))
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    col = symbol.replace('^','').replace('-','_').replace('=','').lower()
    out = df[['Close']].rename(columns={'Close': col})
    out.index = pd.to_datetime(out.index).tz_localize(None)
    return out

try:
    vix_df   = yf_close('^VIX')
    vix3m_df = yf_close('^VIX3M')
    hyg_df   = yf_close('HYG').rename(columns={'hyg':'hyg_price'})
    lqd_df   = yf_close('LQD').rename(columns={'lqd':'lqd_price'})
    print(f'  VIX: {vix_df.iloc[-1].values[0]:.2f}  VIX3M: {vix3m_df.iloc[-1].values[0]:.2f}')
except Exception as e:
    print(f'[ERROR] Core Yahoo fetch failed: {e}')
    sys.exit(1)

# Put/call ratio — try multiple symbols, degrade gracefully if unavailable
pcr_df = None
for pcr_sym in ['^PCALL', '^PCE', '^VXST']:
    try:
        pcr_df = yf_close(pcr_sym).rename(columns={pcr_sym.replace('^','').lower(): 'put_call'})
        print(f'  Put/call ratio: {pcr_sym} ✓')
        break
    except Exception:
        continue
if pcr_df is None:
    print('  Put/call ratio: unavailable — feature excluded')

# SPY via FMP (has get_historical_prices)
try:
    import fmp
    spy_raw = fmp.get_historical_prices('SPY', limit=LOOKBACK)
    spx_df  = pd.DataFrame(spy_raw)[['date','close']].rename(columns={'close':'spx'})
    spx_df['date'] = pd.to_datetime(spx_df['date']).dt.tz_localize(None)
    spx_df = spx_df.set_index('date').sort_index()
    print(f'  SPY: {spx_df.iloc[-1].values[0]:.2f}  ({len(spx_df)} bars)')
except Exception as e:
    print(f'[WARN] FMP SPY fallback to yfinance: {e}')
    spx_df = yf_close('SPY', period='2y').rename(columns={'spy':'spx'})

# Align on common dates
base = (vix_df
    .join(vix3m_df, how='inner')
    .join(hyg_df,   how='inner')
    .join(lqd_df,   how='inner')
    .join(spx_df,   how='inner')
)
if pcr_df is not None:
    base = base.join(pcr_df, how='inner')

features = base.dropna().tail(LOOKBACK)

features['vix_5d_chg']     = features['vix'].diff(5)
features['vix_term_slope'] = features['vix3m'] / features['vix']
features['spx_rv_20d']     = features['spx'].pct_change().rolling(20).std() * np.sqrt(252) * 100
features['hy_ig_spread']   = (features['hyg_price'].pct_change() - features['lqd_price'].pct_change()).rolling(5).mean() * -100
features['spx_5d_return']  = features['spx'].pct_change(5)

FEATURE_COLS = ['vix','vix_5d_chg','vix_term_slope','spx_rv_20d','hy_ig_spread','spx_5d_return']
if pcr_df is not None and 'put_call' in features.columns:
    features['put_call_5d_ma'] = features['put_call'].rolling(5).mean()
    FEATURE_COLS.append('put_call_5d_ma')
X = features[FEATURE_COLS].dropna().values
print(f'  Feature matrix: {X.shape[0]} rows × {X.shape[1]} cols')

# ── Step 2: HMM ───────────────────────────────────────────────────────────────

print('[market-state] Step 2 — HMM state update...')

from hmmlearn import hmm

IS_REFIT_DAY = date.today().weekday() == 0  # Monday
model_path   = MODEL_DIR / 'hmm_model_latest.pkl'

if IS_REFIT_DAY or not model_path.exists():
    print('  Fitting new HMM model (4 states, full covariance)...')
    model = hmm.GaussianHMM(n_components=4, covariance_type='full', n_iter=200,
                             random_state=42, tol=1e-4)
    model.fit(X)
    with open(MODEL_DIR / f'hmm_model_{TODAY}.pkl', 'wb') as f:
        pickle.dump(model, f)
    with open(model_path, 'wb') as f:
        pickle.dump(model, f)
    REFIT_PERFORMED = True
    print('  Model fitted and saved')
else:
    with open(model_path, 'rb') as f:
        model = pickle.load(f)
    REFIT_PERFORMED = False
    print('  Loaded existing model')

state_sequence = model.predict(X)
state_probs    = model.predict_proba(X)

# Map raw states to named regimes by VIX mean
means       = model.means_[:, 0]
state_order = np.argsort(means)
STATE_NAMES = {
    state_order[0]: 'LOW_VOL',
    state_order[1]: 'TRANSITIONING',
    state_order[2]: 'HIGH_VOL',
    state_order[3]: 'CRISIS',
}

current_raw       = int(state_sequence[-1])
current_state     = STATE_NAMES[current_raw]
current_probs_raw = state_probs[-1]
current_probs     = {STATE_NAMES[i]: round(float(current_probs_raw[i]), 4) for i in range(4)}
confidence        = current_probs[current_state]

trans_row   = model.transmat_[current_raw]
trans_named = {STATE_NAMES[i]: round(float(trans_row[i]), 4) for i in range(4)}

days_in_state = 1
for s in reversed(state_sequence[:-1]):
    if STATE_NAMES[int(s)] == current_state:
        days_in_state += 1
    else:
        break

prior_state = 'UNKNOWN'
regime_latest = MODEL_DIR / 'regime_latest.json'
if regime_latest.exists():
    with open(regime_latest) as f:
        prior = json.load(f)
    prior_state = prior.get('state', 'UNKNOWN')
else:
    prior = {}

effective_state = current_state
if confidence < 0.60 and current_state == 'LOW_VOL':
    effective_state = 'TRANSITIONING'
    print(f'  Confidence override: {confidence:.0%} → forcing TRANSITIONING')

print(f'  State: {effective_state} (raw={current_state}, confidence={confidence:.0%})')
print(f'  Days in state: {days_in_state}  |  Prior: {prior_state}')

# ── Step 3: RORO Score ────────────────────────────────────────────────────────

print('[market-state] Step 3 — RORO score...')

def z5(series):
    s = pd.Series(series).dropna()
    if len(s) < 6:
        return 0.0
    ret = s.pct_change(5).iloc[-1]
    std = s.pct_change(5).std()
    return float(ret / std) if std > 0 else 0.0

tlt = yf_close('TLT', '2mo').values.flatten()
hyg = features['hyg_price'].values
lqd = features['lqd_price'].values
btc = yf_close('BTC-USD', '2mo').values.flatten()
iwm = yf_close('IWM', '2mo').values.flatten()
jpy = yf_close('JPY=X', '2mo').values.flatten()
spy = features['spx'].values

roro_components = {
    'spx_vs_tlt':  z5(spy)  - z5(tlt),
    'hyg_vs_lqd':  z5(hyg)  - z5(lqd),
    'jpy_inverse': -z5(jpy),
    'btc_return':  z5(btc)  * 0.5,
    'vix_inverse': -z5(features['vix'].values),
    'iwm_vs_spy':  z5(iwm)  - z5(spy),
}
roro_score = float(np.mean([np.clip(v, -2, 2) for v in roro_components.values()]) * 50)
print(f'  RORO: {roro_score:+.1f}  components: { {k: round(v,2) for k,v in roro_components.items()} }')

# ── Step 4: Stress Score ──────────────────────────────────────────────────────

print('[market-state] Step 4 — Stress score...')

today_features = dict(zip(FEATURE_COLS, X[-1]))

def pct_rank(series, value):
    return float(np.mean(series <= value) * 100)

stress_score = int(
    pct_rank(features['vix'].values,           today_features['vix'])           * 0.25 +
    pct_rank(features['vix_5d_chg'].values,    today_features['vix_5d_chg'])    * 0.15 +
    (100 - pct_rank(features['vix_term_slope'].values, today_features['vix_term_slope'])) * 0.20 +
    pct_rank(features['hy_ig_spread'].values,  today_features['hy_ig_spread'])  * 0.25 +
    (100 - pct_rank(features['spx_5d_return'].values,  today_features['spx_5d_return']))  * 0.15
)
print(f'  Stress: {stress_score}/100')
print(f'  Features: VIX={today_features["vix"]:.1f}, term_slope={today_features["vix_term_slope"]:.3f}, rv20={today_features["spx_rv_20d"]:.1f}%')

# ── Step 5: Strategy Activation ───────────────────────────────────────────────

STRATEGY_MATRIX = {
    'LOW_VOL':       {'S1','S2','S3','S4','S5','S6','S7','S8','S9','S10','S11','S12','S13','S14','S15_SELL','S16','S17','S18','S19','S20'},
    'TRANSITIONING': {'S1','S2','S3','S4','S5','S6','S7','S8','S9','S10','S11','S12','S13','S15_BUY','S16','S17','S18_CONFIRM','S19','S20'},
    'HIGH_VOL':      {'S1','S2','S3','S4','S5','S6','S8','S9','S10','S11','S12','S13','S15_BUY','S16','S17','S18','S19','S20'},
    'CRISIS':        {'S1','S2','S3','S4','S5','S6','S8','S10','S12','S18','S20'},
}
active_strategies = sorted(STRATEGY_MATRIX[effective_state])
position_scale    = {'LOW_VOL':1.0,'TRANSITIONING':0.55,'HIGH_VOL':0.35,'CRISIS':0.15}[effective_state]

print(f'[market-state] Step 5 — {len(active_strategies)} strategies active at {position_scale:.0%} scale')

# ── Step 6: SP100 Signal Blocks ───────────────────────────────────────────────

print('[market-state] Step 6 — SP100 signal blocks...')

SP100 = [
    'AAPL','MSFT','AMZN','NVDA','GOOGL','META','TSLA','BRK-B','UNH','JNJ',
    'XOM','JPM','V','PG','MA','HD','CVX','MRK','ABBV','LLY','PEP','KO','AVGO',
    'COST','WMT','BAC','MCD','CSCO','CRM','ACN','TMO','ABT','NEE','DHR','ADBE',
    'NKE','PM','TXN','WFC','UPS','MS','RTX','BMY','AMGN','ORCL','HON','QCOM',
    'SCHW','LOW','CAT','SBUX','GS','BA','INTU','IBM','GE','AXP','ELV','BLK',
    'MDLZ','GILD','MMM','ADI','DE','ISRG','SYK','REGN','VRTX','ZTS','LMT',
    'CVS','MO','SO','DUK','CL','PLD','AMT','EQIX','NOC','GD','TGT','USB',
    'PNC','TFC','FIS','ETN','MCO','SPG','PSA','EW','KLAC','MCHP','ANET',
    'AFL','AIG','ALL','APD','BK','BSX','CB',
]

os.makedirs('work/market-state/data', exist_ok=True)
signal_blocks = {}
errors = 0

for i, ticker in enumerate(SP100):
    try:
        time.sleep(0.22)  # 300 req/min = 5/s; 3 calls/ticker → ~0.6s/ticker → safe at 0.22s/call
        metrics = fmp.get_key_metrics(ticker, limit=1)
        ratios  = fmp.get_ratios(ticker, limit=1)
        if not metrics or not ratios:
            continue
        m, r = metrics[0], ratios[0]

        # Revenue growth from annual financials (2 periods)
        rev_growth_yoy = 0.0
        try:
            fs = fmp.get_financial_statements(ticker, period='annual', limit=2)
            if fs and len(fs) >= 2:
                rev_now  = fs[0].get('revenue', 0) or 0
                rev_prev = fs[1].get('revenue', 1) or 1
                rev_growth_yoy = round((rev_now / rev_prev - 1) * 100, 2) if rev_prev else 0.0
        except Exception:
            pass

        block = {
            'ticker':             ticker,
            'revenue_growth_yoy': rev_growth_yoy,
            'gross_margin':       round((r.get('grossProfitMargin', 0) or 0) * 100, 2),
            'fcf_yield':          round((m.get('freeCashFlowYield', 0) or 0) * 100, 2),
            'net_debt_to_ebitda': round(float(m.get('netDebtToEBITDA', 0) or 0), 2),
            'roe':                round((m.get('returnOnEquity', 0) or 0) * 100, 2),
            'pe_ratio':           round(float(r.get('priceToEarningsRatio', r.get('priceEarningsRatio', 0)) or 0), 2),
            'signals':            [],
            'confluence':         0,
        }

        if 'S10' in active_strategies:
            quality = (
                int(block['roe'] > 15) + int(block['gross_margin'] > 40) +
                int(block['net_debt_to_ebitda'] < 2) + int(block['fcf_yield'] > 3)
            ) * 25
            block['quality_score'] = quality
            if quality >= 75:
                block['signals'].append('S10_HIGH_QUALITY')

        if block['revenue_growth_yoy'] > 15 and block['net_debt_to_ebitda'] < 3:
            block['signals'].append('GROWTH_QUALITY')
        if effective_state in ('HIGH_VOL','CRISIS') and block['net_debt_to_ebitda'] < 0.5 and block['fcf_yield'] > 7:
            block['signals'].append('CRISIS_QUALITY_VALUE')

        block['confluence'] = len(block['signals'])
        signal_blocks[ticker] = block

        if (i + 1) % 20 == 0:
            print(f'  {i+1}/{len(SP100)} processed...')

    except Exception as e:
        errors += 1
        signal_blocks[ticker] = {'ticker': ticker, 'error': str(e)[:120], 'confluence': 0}

pd.DataFrame(list(signal_blocks.values())).to_csv(f'work/market-state/data/signal_blocks_{TODAY}.csv', index=False)
print(f'  {len(SP100) - errors} tickers processed, {errors} errors')

# ── Step 7: Candidates & Output ───────────────────────────────────────────────

candidates = [
    {'ticker': t, 'signals': b['signals'], 'confluence': b['confluence']}
    for t, b in signal_blocks.items()
    if not b.get('error') and b['confluence'] > 0
]
candidates.sort(key=lambda x: x['confluence'], reverse=True)

with open(MODEL_DIR / 'candidate_queue.json', 'w') as f:
    json.dump(candidates, f, indent=2)

regime_change_alert = (
    current_state != prior_state or
    confidence < 0.60 or
    (stress_score > 50 and prior.get('stress_score', 0) <= 50)
)

output = {
    'date':                      TODAY,
    'state':                     effective_state,
    'state_raw':                 current_state,
    'state_probabilities':       current_probs,
    'confidence':                round(confidence, 4),
    'transition_probs_tomorrow': trans_named,
    'stress_score':              stress_score,
    'roro_score':                round(roro_score, 1),
    'features':                  {k: round(float(v), 4) for k, v in today_features.items()},
    'regime_change_alert':       regime_change_alert,
    'days_in_current_state':     days_in_state,
    'prior_state':               prior_state,
    'active_strategies':         active_strategies,
    'position_scale':            position_scale,
    'candidates_identified':     len(candidates),
    'refit_performed':           REFIT_PERFORMED,
    'notes':                     f'Confidence override → TRANSITIONING' if effective_state != current_state else '',
}

with open(MODEL_DIR / f'regime_{TODAY}.json', 'w') as f:
    json.dump(output, f, indent=2)
with open(MODEL_DIR / 'regime_latest.json', 'w') as f:
    json.dump(output, f, indent=2)
with open(MODEL_DIR / 'latest.json', 'w') as f:
    json.dump(output, f, indent=2)

log_row = f"{TODAY},{effective_state},{stress_score},{round(roro_score,1)},{confidence:.3f},{len(candidates)},{regime_change_alert}\n"
with open(MODEL_DIR / 'regime_log.csv', 'a') as f:
    f.write(log_row)

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"""
{'='*50}
MARKET_STATE_SUMMARY
{'='*50}
date:               {TODAY}
state:              {effective_state}
stress:             {stress_score}/100
roro:               {roro_score:+.1f}
confidence:         {confidence:.0%}
vix:                {today_features['vix']:.2f}
vix_term_slope:     {today_features['vix_term_slope']:.3f}
spx_rv_20d:         {today_features['spx_rv_20d']:.1f}%
days_in_state:      {days_in_state}
position_scale:     {position_scale:.0%}
active_strategies:  {len(active_strategies)} of 20
candidates:         {len(candidates)}
top_5:              {[c['ticker'] for c in candidates[:5]]}
regime_alert:       {regime_change_alert}
refit:              {REFIT_PERFORMED}
{'='*50}
""")
