#!/usr/bin/env python3
"""
TIER_B Data Prep — creates all required workspace data files for a single ticker.
Run from /root/openclaw: python3 scripts/run_tier_b.py TICKER

Outputs (all under work/{TICKER}-diligence/data/):
  financials.csv       — income + key metrics + ratios merged
  prices.parquet       — 252 OHLCV bars from FMP
  comps.csv            — peer EV/revenue and margin comparisons
  insider.csv          — insider transactions (Yahoo)
  options_summary.json — IV30, HV20, put/call ratio
  DATA_MANIFEST.json   — validation gate (required for pipeline to proceed)
"""

import sys, os, json, time
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date

if len(sys.argv) < 2:
    print('Usage: python3 scripts/run_tier_b.py TICKER')
    sys.exit(1)

TICKER = sys.argv[1].upper()
print(f'\n[tier-b] Starting TIER_B data prep for {TICKER} — {date.today().isoformat()}')

TOOLS_DIR = Path('workspaces/default/tools')
sys.path.insert(0, str(TOOLS_DIR))

from dotenv import load_dotenv
load_dotenv('.env')

TASK_DIR = Path(f'workspaces/default/work/{TICKER}-diligence')
DATA_DIR = TASK_DIR / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

import yfinance as yf
import fmp, yahoo, polygon

manifest = {}
errors   = []

# ── 1. Financial Statements (income + balance + cash flow) ───────────────────

print(f'[tier-b] Fetching financials...')
try:
    income  = fmp.get_financial_statements(TICKER, period='quarterly', limit=8)
    balance = fmp.get_balance_sheet(TICKER,         period='quarterly', limit=8)
    cf      = fmp.get_cash_flow(TICKER,             period='quarterly', limit=8)
    metrics = fmp.get_key_metrics(TICKER,           limit=8)
    ratios  = fmp.get_ratios(TICKER,                limit=8)

    if not income:
        raise ValueError('No income statement data returned')

    # Build per-period financials DataFrame
    rows = []
    for i, q in enumerate(income):
        m = metrics[i] if i < len(metrics) else {}
        r = ratios[i]  if i < len(ratios)  else {}
        b = balance[i] if i < len(balance) else {}
        c = cf[i]      if i < len(cf)      else {}

        rev  = q.get('revenue', 0) or 0
        cogs = q.get('costOfRevenue', 0) or 0
        gp   = q.get('grossProfit', rev - cogs) or 0

        # Revenue growth YoY (compare to same quarter prior year = index+4)
        prior_rev = income[i+4].get('revenue', 0) if i + 4 < len(income) else None
        rev_growth = round((rev / prior_rev - 1) * 100, 2) if prior_rev else None

        rows.append({
            'period':           q.get('period', ''),
            'date':             q.get('date', ''),
            'revenue':          rev,
            'gross_profit':     gp,
            'gross_margin':     round(gp / rev * 100, 2) if rev else 0,
            'operating_income': q.get('operatingIncome', 0) or 0,
            'net_income':       q.get('netIncome', 0) or 0,
            'ebitda':           q.get('ebitda', 0) or 0,
            'revenue_growth_yoy': rev_growth,
            'roe':              round((m.get('returnOnEquity', 0) or 0) * 100, 2),
            'roa':              round((m.get('returnOnAssets', 0) or 0) * 100, 2),
            'fcf_yield':        round((m.get('freeCashFlowYield', 0) or 0) * 100, 2),
            'ev_to_ebitda':     round(m.get('evToEBITDA', 0) or 0, 2),
            'net_debt_ebitda':  round(m.get('netDebtToEBITDA', 0) or 0, 2),
            'gross_profit_margin': round((r.get('grossProfitMargin', 0) or 0) * 100, 2),
            'operating_margin': round((r.get('operatingProfitMargin', 0) or 0) * 100, 2),
            'net_margin':       round((r.get('netProfitMargin', 0) or 0) * 100, 2),
            'current_ratio':    round(r.get('currentRatio', 0) or 0, 2),
            'debt_to_equity':   round(r.get('debtToEquityRatio', 0) or 0, 2),
            'pe_ratio':         round(r.get('priceToEarningsRatio', 0) or 0, 2),
            'ps_ratio':         round(r.get('priceToSalesRatio', 0) or 0, 2),
            'total_assets':     b.get('totalAssets', 0) or 0,
            'total_debt':       b.get('totalDebt', 0) or 0,
            'cash':             b.get('cashAndCashEquivalents', 0) or 0,
            'free_cash_flow':   c.get('freeCashFlow', 0) or 0,
            'capex':            c.get('capitalExpenditure', 0) or 0,
        })

    fin_df = pd.DataFrame(rows)
    fin_df.to_csv(DATA_DIR / 'financials.csv', index=False)
    manifest['financials.csv'] = {'rows': len(fin_df), 'fields': list(fin_df.columns)}
    print(f'  financials.csv — {len(fin_df)} quarters')
    time.sleep(0.25)

except Exception as e:
    errors.append(f'financials: {e}')
    print(f'  ERROR: {e}')

# ── 2. Price History ──────────────────────────────────────────────────────────

print(f'[tier-b] Fetching price history...')
try:
    prices_raw = fmp.get_historical_prices(TICKER, limit=252)
    if not prices_raw:
        raise ValueError('No price data returned')

    prices_df = pd.DataFrame(prices_raw)[['date','open','high','low','close','volume']]
    prices_df['date'] = pd.to_datetime(prices_df['date'])
    prices_df = prices_df.sort_values('date').reset_index(drop=True)

    # Add derived columns used by research and compute
    prices_df['returns']      = prices_df['close'].pct_change()
    prices_df['returns_5d']   = prices_df['close'].pct_change(5)
    prices_df['hv20']         = prices_df['returns'].rolling(20).std() * np.sqrt(252) * 100
    prices_df['sma20']        = prices_df['close'].rolling(20).mean()
    prices_df['sma50']        = prices_df['close'].rolling(50).mean()
    prices_df['high_52w']     = prices_df['close'].rolling(252).max()
    prices_df['vs_52w_high']  = (prices_df['close'] / prices_df['high_52w'] - 1) * 100

    prices_df.to_parquet(DATA_DIR / 'prices.parquet', index=False)
    manifest['prices.parquet'] = {'rows': len(prices_df), 'fields': list(prices_df.columns)}
    print(f'  prices.parquet — {len(prices_df)} bars  current=${prices_df["close"].iloc[-1]:.2f}')
    time.sleep(0.25)

except Exception as e:
    errors.append(f'prices: {e}')
    print(f'  ERROR: {e}')

# ── 3. Peer Comparisons ───────────────────────────────────────────────────────

print(f'[tier-b] Fetching peer comps...')
try:
    peers = fmp.get_peers(TICKER) or []
    if isinstance(peers, dict):
        peers = peers.get('peersList', [])

    # Build comps including the ticker itself
    all_tickers = [TICKER] + [p for p in peers[:3] if isinstance(p, str)]
    comp_rows   = []

    for t in all_tickers:
        try:
            m = fmp.get_key_metrics(t, limit=1)
            r = fmp.get_ratios(t, limit=1)
            q = fmp.get_quote(t)
            if not m: continue
            m, r = m[0], (r[0] if r else {})
            mktcap = q.get('marketCap', 0) or 0
            rev    = None
            try:
                fs  = fmp.get_financial_statements(t, period='annual', limit=1)
                rev = fs[0].get('revenue', 0) if fs else None
            except Exception:
                pass
            ev_rev = round(m.get('evToEBITDA', 0) * 0.3 + (mktcap / rev if rev else 0), 2) if rev else None
            comp_rows.append({
                'ticker':       t,
                'ev_revenue':   ev_rev,
                'ev_ebitda':    round(m.get('evToEBITDA', 0) or 0, 2),
                'pe':           round(r.get('priceToEarningsRatio', 0) or 0, 2),
                'gross_margin': round((r.get('grossProfitMargin', 0) or 0) * 100, 2),
                'fcf_yield':    round((m.get('freeCashFlowYield', 0) or 0) * 100, 2),
                'roe':          round((m.get('returnOnEquity', 0) or 0) * 100, 2),
                'mktcap_b':     round(mktcap / 1e9, 1),
            })
            time.sleep(0.25)
        except Exception:
            pass

    comps_df = pd.DataFrame(comp_rows)
    comps_df.to_csv(DATA_DIR / 'comps.csv', index=False)
    manifest['comps.csv'] = {'rows': len(comps_df), 'fields': list(comps_df.columns)}
    print(f'  comps.csv — {len(comps_df)} peers')

except Exception as e:
    errors.append(f'comps: {e}')
    print(f'  ERROR: {e}')

# ── 4. Insider Transactions ───────────────────────────────────────────────────

print(f'[tier-b] Fetching insider transactions...')
try:
    insider_raw = yahoo.get_insider_transactions(TICKER)
    if insider_raw and isinstance(insider_raw, dict):
        # Yahoo returns nested structure
        txns = insider_raw.get('transactions', insider_raw.get('insiderTransactions', []))
        if isinstance(txns, dict):
            txns = txns.get('transactions', [])
    elif isinstance(insider_raw, list):
        txns = insider_raw
    else:
        txns = []

    if txns:
        insider_df = pd.DataFrame(txns)
        # Normalize column names
        col_map = {
            'startDate':        'transaction_date',
            'filerName':        'name',
            'transactionText':  'description',
            'shares':           'shares',
            'value':            'value',
            'ownership':        'ownership_type',
        }
        insider_df = insider_df.rename(columns={k: v for k, v in col_map.items() if k in insider_df.columns})
        if 'transaction_date' in insider_df.columns:
            insider_df['transaction_date'] = pd.to_datetime(insider_df['transaction_date'], unit='s', errors='coerce').dt.strftime('%Y-%m-%d')
        # Infer transaction_type from description or ownership
        if 'transaction_type' not in insider_df.columns:
            if 'description' in insider_df.columns:
                insider_df['transaction_type'] = insider_df['description'].str.extract(r'(Sale|Purchase|Grant)', expand=False).map({'Sale':'S','Purchase':'P','Grant':'G'}).fillna('U')
            else:
                insider_df['transaction_type'] = 'U'
        # Value: negative = sale
        if 'value' in insider_df.columns and 'transaction_type' in insider_df.columns:
            insider_df.loc[insider_df['transaction_type'] == 'S', 'value'] = -insider_df.loc[insider_df['transaction_type'] == 'S', 'value'].abs()
        insider_df.to_csv(DATA_DIR / 'insider.csv', index=False)
        manifest['insider.csv'] = {'rows': len(insider_df), 'fields': list(insider_df.columns)}
        print(f'  insider.csv — {len(insider_df)} transactions')
    else:
        pd.DataFrame(columns=['transaction_date','name','transaction_type','shares','value']).to_csv(DATA_DIR / 'insider.csv', index=False)
        manifest['insider.csv'] = {'rows': 0, 'fields': ['transaction_date','name','transaction_type','shares','value']}
        print(f'  insider.csv — no transactions found (empty)')

except Exception as e:
    errors.append(f'insider: {e}')
    print(f'  insider: unavailable ({e}) — writing empty')
    pd.DataFrame(columns=['transaction_date','name','transaction_type','shares','value']).to_csv(DATA_DIR / 'insider.csv', index=False)
    manifest['insider.csv'] = {'rows': 0, 'fields': []}

# ── 5. Options Summary ────────────────────────────────────────────────────────

print(f'[tier-b] Fetching options chain...')
options_summary = {}
try:
    tk   = yf.Ticker(TICKER)
    exps = tk.options  # tuple of expiration date strings
    if not exps:
        raise ValueError('No options expirations found')

    # Use nearest expiration (~30d if available, else first)
    target_exp = exps[0]
    for exp in exps:
        import datetime
        days = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
        if days >= 25:
            target_exp = exp
            break

    chain = tk.option_chain(target_exp)
    calls = chain.calls
    puts  = chain.puts

    # Current price from prices.parquet (already written in step 2)
    prices_pq = DATA_DIR / 'prices.parquet'
    prices_df = pd.read_parquet(prices_pq) if prices_pq.exists() else None
    current   = float(prices_df['close'].iloc[-1]) if prices_df is not None else 0

    # ATM IV from nearest 3 call strikes
    if not calls.empty and 'strike' in calls.columns and 'impliedVolatility' in calls.columns:
        calls = calls.copy()
        calls['dist'] = (calls['strike'].astype(float) - current).abs()
        atm  = calls.nsmallest(3, 'dist')
        iv30 = float(atm['impliedVolatility'].mean()) * 100
    else:
        iv30 = 0

    hv20     = float(prices_df['hv20'].iloc[-1]) if prices_df is not None and 'hv20' in prices_df.columns else 0
    put_vol  = float(puts['volume'].fillna(0).sum())  if 'volume' in puts.columns  else 0
    call_vol = float(calls['volume'].fillna(0).sum()) if 'volume' in calls.columns else 1

    options_summary = {
        'iv30':               round(iv30, 2),
        'hv20':               round(hv20, 2),
        'iv_rv_spread':       round(iv30 - hv20, 2),
        'put_call_vol_ratio': round(put_vol / max(call_vol, 1), 3),
        'expiration_used':    target_exp,
        'n_calls':            len(calls),
        'n_puts':             len(puts),
    }
    print(f'  options_summary.json — IV30={iv30:.1f} HV20={hv20:.1f} spread={iv30-hv20:+.1f}  calls={len(calls)} puts={len(puts)}')

except Exception as e:
    print(f'  options: unavailable ({e}) — writing empty')

with open(DATA_DIR / 'options_summary.json', 'w') as f:
    json.dump(options_summary, f)
manifest['options_summary.json'] = {'rows': 1, 'fields': list(options_summary.keys())}

# ── 6. Profile + Price Target ─────────────────────────────────────────────────

print(f'[tier-b] Fetching profile + price targets...')
try:
    profile = fmp.get_profile(TICKER)
    pt      = fmp.get_price_target(TICKER)
    if profile:
        with open(DATA_DIR / 'profile.json', 'w') as f:
            json.dump(profile, f, indent=2)
        manifest['profile.json'] = {'rows': 1, 'fields': list(profile.keys()) if isinstance(profile, dict) else []}
        sector   = profile.get('sector', 'Unknown') if isinstance(profile, dict) else 'Unknown'
        industry = profile.get('industry', 'Unknown') if isinstance(profile, dict) else 'Unknown'
        print(f'  profile.json — {sector} / {industry}')
    if pt:
        with open(DATA_DIR / 'price_targets.json', 'w') as f:
            json.dump(pt, f, indent=2)
        manifest['price_targets.json'] = {'rows': 1, 'fields': list(pt.keys()) if isinstance(pt, dict) else []}
except Exception as e:
    print(f'  profile/targets: {e}')

# ── 7. Merge into Master Dataset ─────────────────────────────────────────────
# The master dataset is the single source of truth — all collected data goes here.
# Strategies and future runs always read from master, never from work directories.

MASTER_DIR = Path('workspaces/default/data/master')
MASTER_DIR.mkdir(parents=True, exist_ok=True)

try:
    # Prices → merge into master prices.parquet
    prices_file = DATA_DIR / 'prices.parquet'
    if prices_file.exists():
        new_prices = pd.read_parquet(prices_file)[['date','open','high','low','close','volume']]
        new_prices['ticker'] = TICKER
        new_prices['date']   = new_prices['date'].astype(str).str[:10]
        master_prices_path   = MASTER_DIR / 'prices.parquet'
        if master_prices_path.exists():
            existing = pd.read_parquet(master_prices_path)
            combined = pd.concat([existing, new_prices], ignore_index=True)
            combined = combined.drop_duplicates(subset=['date','ticker']).sort_values(['ticker','date'])
        else:
            combined = new_prices.sort_values(['ticker','date'])
        combined.to_parquet(master_prices_path, index=False)
        print(f'  [master] prices: {TICKER} merged → {len(combined):,} total rows, {combined["ticker"].nunique()} tickers')
except Exception as e:
    print(f'  [master] prices merge error: {e}')

try:
    # Financials → merge into master financials.parquet
    fin_file = DATA_DIR / 'financials.csv'
    if fin_file.exists():
        new_fin = pd.read_csv(fin_file)
        new_fin['ticker'] = TICKER
        master_fin_path   = MASTER_DIR / 'financials.parquet'
        if master_fin_path.exists():
            existing_fin = pd.read_parquet(master_fin_path)
            # Remove old rows for this ticker, replace with fresh
            existing_fin = existing_fin[existing_fin['ticker'] != TICKER]
            combined_fin = pd.concat([existing_fin, new_fin], ignore_index=True)
        else:
            combined_fin = new_fin
        combined_fin.to_parquet(master_fin_path, index=False)
        print(f'  [master] financials: {TICKER} merged → {combined_fin["ticker"].nunique()} tickers')
except Exception as e:
    print(f'  [master] financials merge error: {e}')

try:
    # Options → merge into master options_eod.parquet
    opts_file = DATA_DIR / 'options_summary.json'
    if opts_file.exists():
        with open(opts_file) as f:
            opts_data = json.load(f)
        if opts_data:
            opts_row = pd.DataFrame([{
                'ticker':             TICKER,
                'date':               date.today().isoformat(),
                'iv30':               opts_data.get('iv30', 0),
                'hv20':               opts_data.get('hv20', 0),
                'iv_rv_spread':       opts_data.get('iv_rv_spread', 0),
                'put_call_vol_ratio': opts_data.get('put_call_vol_ratio', 0),
                'expiration_used':    opts_data.get('expiration_used', ''),
            }])
            master_opts_path = MASTER_DIR / 'options_summary.parquet'
            if master_opts_path.exists():
                existing_opts = pd.read_parquet(master_opts_path)
                existing_opts = existing_opts[~((existing_opts['ticker']==TICKER) & (existing_opts['date']==date.today().isoformat()))]
                combined_opts = pd.concat([existing_opts, opts_row], ignore_index=True)
            else:
                combined_opts = opts_row
            combined_opts.to_parquet(master_opts_path, index=False)
            print(f'  [master] options_summary: {TICKER} merged')
except Exception as e:
    print(f'  [master] options merge error: {e}')

# ── 8. DATA_MANIFEST.json ─────────────────────────────────────────────────────

required_files = ['financials.csv', 'prices.parquet', 'comps.csv']
missing        = [f for f in required_files if f not in manifest]

manifest_out = {
    'ticker':            TICKER,
    'date':              date.today().isoformat(),
    'files':             manifest,
    'required_present':  len(missing) == 0,
    'missing_required':  missing,
    'errors':            errors,
    'status':            'READY' if not missing else 'INCOMPLETE',
}

with open(DATA_DIR / 'DATA_MANIFEST.json', 'w') as f:
    json.dump(manifest_out, f, indent=2)

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"""
{'='*50}
TIER_B COMPLETE — {TICKER}
{'='*50}
Status:   {manifest_out['status']}
Files:    {len(manifest)} created
Required: {'✅ ALL PRESENT' if not missing else '❌ MISSING: ' + ', '.join(missing)}
Errors:   {len(errors)}
{'='*50}
""")

if missing:
    print('Pipeline BLOCKED — required files missing. Check errors above.')
    sys.exit(1)

sys.exit(0)
