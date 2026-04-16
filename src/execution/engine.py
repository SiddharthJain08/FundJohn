#!/usr/bin/env python3
"""
OpenClaw Execution Engine — zero-token, zero-LLM.

Daily run sequence:
  1. Load regime state from DB
  2. Load approved strategies from strategy_registry
  3. Load prices + aux_data
  4. Run each strategy → collect signals
  5. Write signals to execution_signals (ON CONFLICT DO NOTHING)
  6. Detect confluence (≥2 strategies agree on same ticker/direction)
  7. Update P&L on open signals
  8. Fire report triggers (stop hit, target hit, 10% drawdown, etc.)
  9. Log execution run metrics
"""

import os
import sys
import json
import logging
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
import pandas as pd
import numpy as np

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from strategies.registry import get_approved_strategies

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ENGINE] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

WORKSPACE = os.environ.get('WORKSPACE_ID', 'default')
DB_URI    = os.environ.get('POSTGRES_URI')

# Trigger thresholds
STOP_TRIGGER_PCT        = -0.02   # -2% below stop = close signal
TARGET1_TRIGGER_PCT     =  0.005  # within 0.5% of target_1
DRAWDOWN_REPORT_PCT     = -0.10   # -10% unrealized triggers review
DAYS_HELD_REPORT        = 30      # flag if held > 30 days with no target hit
CONFLUENCE_MIN          = 2       # min strategies agreeing for confluence


def get_db():
    return psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.DictCursor)


# ──────────────────────────────────────────────────────────
# 1. LOAD REGIME
# ──────────────────────────────────────────────────────────

def resolve_workspace(cur, name_or_id: str) -> str:
    """Resolve workspace name ('default') to UUID."""
    if len(name_or_id) == 36 and '-' in name_or_id:
        return name_or_id  # already a UUID
    cur.execute("SELECT id FROM workspaces WHERE name=%s LIMIT 1", (name_or_id,))
    row = cur.fetchone()
    if row:
        return str(row['id'])
    cur.execute("SELECT id FROM workspaces ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    return str(row['id']) if row else name_or_id


def load_regime(cur) -> dict:
    cur.execute("""
        SELECT state, vix_level, vix_percentile, regime_data, updated_at
        FROM market_regime
        ORDER BY updated_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        logger.warning("No regime row found — defaulting to HIGH_VOL")
        return {'state': 'HIGH_VOL', 'vix_level': 25.0}
    return {
        'state':           row['state'],
        'vix_level':       float(row['vix_level'] or 25),
        'vix_percentile':  float(row['vix_percentile'] or 50),
        'regime_data':     row['regime_data'] or {},
        'updated_at':      str(row['updated_at']),
    }


# ──────────────────────────────────────────────────────────
# 2. LOAD STRATEGIES
# ──────────────────────────────────────────────────────────

def load_approved_strategies(cur):
    cur.execute("""
        SELECT id, name, parameters, status, regime_conditions, universe
        FROM strategy_registry
        WHERE status = 'approved'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    logger.info(f"Found {len(rows)} approved strategies in DB")
    return get_approved_strategies(rows)


# ──────────────────────────────────────────────────────────
# 3. LOAD PRICES
# ──────────────────────────────────────────────────────────

def load_prices(universe: list) -> pd.DataFrame:
    """Load master price parquet; pivot to wide format (date index × ticker columns, close prices)."""
    master_path = ROOT / 'data' / 'master' / 'prices.parquet'
    if not master_path.exists():
        logger.warning(f"Master prices not found at {master_path}")
        return pd.DataFrame()
    try:
        df = pd.read_parquet(master_path, columns=['ticker', 'date', 'close'])
        wide = df.pivot(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide.sort_index(inplace=True)
        cols = [c for c in universe if c in wide.columns]
        if cols:
            wide = wide[cols]
        logger.info(f"Prices loaded: {wide.shape[1]} tickers × {wide.shape[0]} dates")
        return wide
    except Exception as e:
        logger.error(f"Failed to load prices: {e}")
        return pd.DataFrame()


def load_aux_data(universe: list) -> dict:
    """Load financials + insider from master Parquets; convert to dict formats the strategies expect."""
    aux = {}
    master_dir = ROOT / 'data' / 'master'

    # Financials: {ticker: {gross_margin, net_margin, ev_ebitda, pe_ratio, ...}}
    # Also provides camelCase aliases for S10_quality_value (FMP field name convention):
    #   roe → returnOnEquity, roic → returnOnInvestedCapital,
    #   gross_margin → grossProfitMargin, debt_equity_ratio → debtEquityRatio,
    #   ev_ebitda → enterpriseValueMultiple, p_fcf_ratio → priceToFreeCashFlowsRatio
    fin_path = master_dir / 'financials.parquet'
    if fin_path.exists():
        try:
            fin = pd.read_parquet(fin_path)
            # Use most recent row per ticker
            fin_latest = fin.sort_values('date').groupby('ticker').last().reset_index()
            fin_dict = {}
            for _, row in fin_latest.iterrows():
                ticker = row.get('ticker')
                if ticker and ticker in universe:
                    d = {
                        k: (float(v) if pd.notna(v) else None)
                        for k, v in row.items()
                        if k not in ('ticker', 'date', 'period')
                    }
                    # camelCase aliases so S10 can use FMP field names directly
                    d['returnOnEquity']             = d.get('roe')
                    d['returnOnInvestedCapital']     = d.get('roic')
                    d['grossProfitMargin']           = d.get('gross_margin')
                    d['debtEquityRatio']             = d.get('debt_equity_ratio')
                    d['enterpriseValueMultiple']     = d.get('ev_ebitda')
                    d['priceToFreeCashFlowsRatio']   = d.get('p_fcf_ratio')
                    fin_dict[ticker] = d
            aux['financials'] = fin_dict
            logger.info(f"Financials loaded: {len(fin_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load financials: {e}")

    # Insider transactions: {ticker: [{transactionDate, transactionType, reportingName, value, shares}]}
    insider_path = master_dir / 'insider.parquet'
    if insider_path.exists():
        try:
            ins = pd.read_parquet(insider_path)
            ins_dict = {}
            for ticker, grp in ins.groupby('ticker'):
                if ticker not in universe:
                    continue
                txns = []
                for _, row in grp.iterrows():
                    txns.append({
                        'transactionDate': str(row.get('date', '')),
                        'transactionType': str(row.get('transaction_type', '')),
                        'reportingName':   str(row.get('insider_name', '')),
                        'value':           float(row.get('net_value', 0) or 0),
                        'shares':          float(row.get('shares', 0) or 0),
                    })
                ins_dict[ticker] = txns
            aux['insider_txns'] = ins_dict
            logger.info(f"Insider data loaded: {len(ins_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load insider: {e}")

    # Options: {ticker: {iv_rank, open_interest_by_strike: {strike: oi}, expiry_date}}
    # Picks nearest future expiry per ticker; sums call+put OI per strike.
    # iv_rank = percentile of current ATM IV vs trailing 30-day history of that ticker's ATM IV.
    opts_path = master_dir / 'options_eod.parquet'
    if opts_path.exists():
        try:
            opts = pd.read_parquet(opts_path)
            today = pd.Timestamp.today().normalize()

            # Ensure expiry is datetime
            if 'expiry' in opts.columns:
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')
            elif 'expiration_date' in opts.columns:
                opts = opts.rename(columns={'expiration_date': 'expiry'})
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')

            if 'date' in opts.columns:
                opts['date'] = pd.to_datetime(opts['date'], errors='coerce')

            opts_dict = {}
            for ticker, grp in opts.groupby('ticker'):
                if ticker not in universe:
                    continue

                # Nearest future expiry with DTE ≤ 45
                future = grp[grp['expiry'] >= today].copy()
                if future.empty:
                    continue
                future['dte'] = (future['expiry'] - today).dt.days
                near = future[future['dte'] <= 45]
                if near.empty:
                    near = future
                nearest_expiry = near['expiry'].min()
                chain = near[near['expiry'] == nearest_expiry]

                # Sum call + put OI per strike
                oi_by_strike = (
                    chain.groupby('strike')['open_interest']
                    .sum()
                    .to_dict()
                )
                # Filter zero-OI strikes
                oi_by_strike = {float(k): float(v) for k, v in oi_by_strike.items() if v and v > 0}

                # IV rank: percentile of today's ATM IV vs trailing 30 days of ATM IV
                iv_rank = 50.0  # default
                if 'implied_volatility' in grp.columns and 'date' in grp.columns:
                    # ATM = strike closest to the most recent close price
                    # Use the current chain to get current ATM IV
                    if 'close' in chain.columns:
                        current_price = chain['close'].iloc[0]
                    else:
                        # Estimate ATM as midpoint of strikes
                        current_price = float(np.median(list(oi_by_strike.keys()))) if oi_by_strike else None

                    if current_price and oi_by_strike:
                        closest_strike = min(oi_by_strike.keys(), key=lambda k: abs(k - current_price))
                        atm_today = chain[chain['strike'].apply(lambda s: abs(float(s) - closest_strike) < 0.01)]
                        current_iv = float(atm_today['implied_volatility'].mean()) if not atm_today.empty else None

                        if current_iv is not None:
                            # 30-day history of this ticker's avg IV
                            cutoff = today - pd.Timedelta(days=30)
                            hist = grp[grp['date'] >= cutoff]
                            daily_iv = hist.groupby('date')['implied_volatility'].mean().dropna()
                            if len(daily_iv) >= 5:
                                lo, hi = daily_iv.min(), daily_iv.max()
                                iv_rank = float(round((current_iv - lo) / (hi - lo) * 100, 1)) if hi > lo else 50.0

                # iv30: mean implied_volatility across the nearest-expiry chain (raw IV, not percentile)
                iv30 = None
                if 'implied_volatility' in chain.columns:
                    iv_vals = chain['implied_volatility'].dropna()
                    if not iv_vals.empty:
                        iv30 = float(iv_vals.mean())

                # volume: total options volume for this ticker's nearest-expiry chain
                chain_volume = 0.0
                if 'volume' in chain.columns:
                    chain_volume = float(chain['volume'].fillna(0).sum())

                #  HV-strategy enrichments 
                # pc_ratio: put/call volume ratio (most recent date)
                pc_ratio = None
                if 'option_type' in chain.columns and 'volume' in chain.columns:
                    if 'date' in chain.columns:
                        latest_dt = chain['date'].max()
                        today_chain = chain[chain['date'] == latest_dt]
                    else:
                        today_chain = chain
                    c_v = float(today_chain[today_chain['option_type'].str.upper() == 'CALL']['volume'].fillna(0).sum())
                    p_v = float(today_chain[today_chain['option_type'].str.upper() == 'PUT']['volume'].fillna(0).sum())
                    pc_ratio = round(p_v / c_v, 4) if c_v > 0 else None

                # gamma_atm: mean gamma of near-ATM options (|delta| 0.40-0.60)
                gamma_atm = None
                if 'delta' in chain.columns and 'gamma' in chain.columns:
                    atm_src = chain[chain['date'] == chain['date'].max()] if 'date' in chain.columns else chain
                    atm_opts = atm_src[atm_src['delta'].abs().between(0.40, 0.60)]
                    if not atm_opts.empty:
                        gamma_atm = round(float(atm_opts['gamma'].mean()), 6)


                # theta_atm: mean theta of near-ATM options (|delta| 0.40-0.60)
                theta_atm = None
                if 'delta' in chain.columns and 'theta' in chain.columns:
                    atm_src2 = chain[chain['date'] == chain['date'].max()] if 'date' in chain.columns else chain
                    atm_opts2 = atm_src2[atm_src2['delta'].abs().between(0.40, 0.60)]
                    if not atm_opts2.empty:
                        theta_atm = round(float(atm_opts2['theta'].mean()), 6)
                # rv_20: current HV20; vrp: implied vol premium over realized vol
                rv_20 = None
                if 'hv20' in grp.columns:
                    latest_hv = grp[grp['date'] == grp['date'].max()]['hv20'].dropna() if 'date' in grp.columns else grp['hv20'].dropna()
                    if not latest_hv.empty:
                        rv_20 = round(float(latest_hv.mean()), 4)
                vrp = round(iv30 - rv_20, 4) if (iv30 is not None and rv_20 is not None) else None

                # History arrays (last 8 trading days)
                iv_rank_history = []; pc_ratio_history = []; vrp_history = []; hv20_history = []
                if 'date' in grp.columns:
                    for d in sorted(grp['date'].unique())[-8:]:
                        day = grp[grp['date'] == d]
                        if 'implied_volatility' in day.columns:
                            day_iv = float(day['implied_volatility'].mean())
                            hist_iv = grp[grp['date'] <= d].groupby('date')['implied_volatility'].mean().dropna()
                            if len(hist_iv) >= 5:
                                lo_d, hi_d = float(hist_iv.min()), float(hist_iv.max())
                                iv_rank_history.append(round((day_iv-lo_d)/(hi_d-lo_d)*100,1) if hi_d>lo_d else 50.0)
                        if 'option_type' in day.columns and 'volume' in day.columns:
                            c_dv = float(day[day['option_type'].str.upper()=='CALL']['volume'].fillna(0).sum())
                            p_dv = float(day[day['option_type'].str.upper()=='PUT']['volume'].fillna(0).sum())
                            pc_ratio_history.append(round(p_dv/c_dv,4) if c_dv>0 else None)
                        if 'hv20' in day.columns:
                            hv_d = day['hv20'].dropna()
                            if not hv_d.empty: hv20_history.append(round(float(hv_d.mean()),4))
                        if 'implied_volatility' in day.columns and 'hv20' in day.columns:
                            hv_d2 = day['hv20'].dropna()
                            if not hv_d2.empty: vrp_history.append(round(float(day['implied_volatility'].mean())-float(hv_d2.mean()),4))
                # 


                #  S-HV13: iv_spread (call_iv - put_iv, ATM, front-month) 
                iv_spread = None
                if 'date' in chain.columns and 'option_type' in chain.columns and 'delta' in chain.columns:
                    import pandas as _pd
                    _ld = chain['date'].max()
                    _td = chain[chain['date'] == _ld].copy()
                    _td['_dte'] = (_pd.to_datetime(_td['expiry']) - _pd.to_datetime(_ld)).dt.days if 'expiry' in _td.columns else 999
                    _fm = _td[_td['_dte'].between(5, 40)]
                    _calls_atm = _fm[(_fm['option_type'].str.upper()=='CALL') & (_fm['delta'].between(0.40,0.60))]
                    _puts_atm  = _fm[(_fm['option_type'].str.upper()=='PUT')  & (_fm['delta'].abs().between(0.40,0.60))]
                    if not _calls_atm.empty and not _puts_atm.empty:
                        iv_spread = round(float(_calls_atm['implied_volatility'].mean()) - float(_puts_atm['implied_volatility'].mean()), 4)

                #  S-HV14: skew_20d (20-delta put IV - 50-delta call IV, smirk) 
                skew_20d = None
                if 'date' in chain.columns and 'delta' in chain.columns and 'option_type' in chain.columns:
                    _ld2 = chain['date'].max()
                    _td2 = chain[chain['date'] == _ld2]
                    _otm_puts = _td2[(_td2['option_type'].str.upper()=='PUT') & (_td2['delta'].between(-0.25,-0.15))]
                    _atm_calls = _td2[(_td2['option_type'].str.upper()=='CALL') & (_td2['delta'].between(0.45,0.55))]
                    if not _otm_puts.empty and not _atm_calls.empty:
                        skew_20d = round(float(_otm_puts['implied_volatility'].mean()) - float(_atm_calls['implied_volatility'].mean()), 4)

                #  S-HV15: term structure (near_iv / far_iv) 
                near_iv_ts = None; far_iv_ts = None; ts_ratio = None
                if 'date' in chain.columns and 'delta' in chain.columns and 'expiry' in chain.columns:
                    import pandas as _pd2
                    _ld3 = chain['date'].max()
                    _td3 = chain[chain['date'] == _ld3].copy()
                    _td3['_dte3'] = (_pd2.to_datetime(_td3['expiry']) - _pd2.to_datetime(_ld3)).dt.days
                    _atm3 = _td3[_td3['delta'].abs().between(0.40, 0.60)]
                    _near = _atm3[_atm3['_dte3'].between(5, 35)]
                    _far  = _atm3[_atm3['_dte3'].between(55, 95)]
                    if not _near.empty and not _far.empty:
                        near_iv_ts = round(float(_near['implied_volatility'].mean()), 4)
                        far_iv_ts  = round(float(_far['implied_volatility'].mean()), 4)
                        ts_ratio   = round(near_iv_ts / far_iv_ts, 4) if far_iv_ts > 0 else None

                #  S-HV16: gex (net dealer gamma exposure) 
                gex = None
                if 'gamma' in chain.columns and 'open_interest' in chain.columns and 'option_type' in chain.columns:
                    _ld4 = chain['date'].max() if 'date' in chain.columns else None
                    _td4 = chain[chain['date'] == _ld4] if _ld4 is not None else chain
                    _c4  = _td4[_td4['option_type'].str.upper() == 'CALL']
                    _p4  = _td4[_td4['option_type'].str.upper() == 'PUT']
                    _gc  = float((_c4['gamma'] * _c4['open_interest']).sum())
                    _gp  = float((_p4['gamma'] * _p4['open_interest']).sum())
                    gex  = round((_gc - _gp) * 100, 2)   # per 1-point move, scaled

                #  S-HV19: iv_centroid_delta + surface_premium 
                iv_centroid_delta = None; surface_premium = None
                if all(c in chain.columns for c in ['vega','delta','open_interest','implied_volatility']):
                    _ld5 = chain['date'].max() if 'date' in chain.columns else None
                    _td5 = chain[chain['date'] == _ld5] if _ld5 is not None else chain
                    _td5 = _td5.copy()
                    _td5['_w'] = _td5['vega'].abs() * _td5['open_interest']
                    _tw = float(_td5['_w'].sum())
                    if _tw > 0:
                        iv_centroid_delta = round(float((_td5['delta'] * _td5['_w']).sum() / _tw), 4)
                        _vwiv = float((_td5['implied_volatility'] * _td5['_w']).sum() / _tw)
                        _atm5 = _td5[_td5['delta'].abs().between(0.45, 0.55)]
                        _atm_iv5 = float(_atm5['implied_volatility'].mean()) if not _atm5.empty else _vwiv
                        surface_premium = round(_vwiv - _atm_iv5, 4)

                opts_dict[ticker] = {
                    'iv_rank':                 iv_rank,
                    'iv30':                    iv30,
                    'volume':                  chain_volume,
                    'open_interest_by_strike': oi_by_strike,
                    'expiry_date':             nearest_expiry.date().isoformat(),
                    'pc_ratio':               pc_ratio,
                    'gamma_atm':              gamma_atm,
                'theta_atm':             theta_atm,
                'iv_spread':           iv_spread,
                'skew_20d':            skew_20d,
                'ts_ratio':            ts_ratio,
                'near_iv':             near_iv_ts,
                'far_iv':              far_iv_ts,
                'gex':                 gex,
                'iv_centroid_delta':   iv_centroid_delta,
                'surface_premium':     surface_premium,
                    'rv_20':                  rv_20,
                    'vrp':                    vrp,
                    'iv_rank_history':         iv_rank_history,
                    'pc_ratio_history':       pc_ratio_history,
                    'vrp_history':            vrp_history,
                    'hv20_history':           hv20_history,
                }

            aux['options'] = opts_dict
            logger.info(f"Options loaded: {len(opts_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load options: {e}")

    return aux


# ──────────────────────────────────────────────────────────
# 4. RUN STRATEGIES
# ──────────────────────────────────────────────────────────

def run_strategies(strategies, prices, regime, universe, aux_data) -> dict:
    """
    Returns: {strategy_id: [Signal, ...]}
    """
    results = {}
    for strat in strategies:
        try:
            signals = strat.generate_signals(prices, regime, universe, aux_data)
            results[strat.id] = signals or []
            logger.info(f"  {strat.id}: {len(results[strat.id])} signals")
        except Exception as e:
            logger.error(f"  {strat.id} FAILED: {e}\n{traceback.format_exc()}")
            results[strat.id] = []
    return results


# ──────────────────────────────────────────────────────────
# 5. WRITE SIGNALS
# ──────────────────────────────────────────────────────────

def write_signals(cur, strategy_results: dict, regime_state: str, run_date: date) -> int:
    total = 0
    for strategy_id, signals in strategy_results.items():
        for sig in signals:
            try:
                # Serialize signal_params — convert numpy scalars to native Python
                def _to_native(v):
                    if hasattr(v, 'item'):
                        return v.item()   # numpy scalar → Python scalar
                    return v
                params_clean = {k: _to_native(v) for k, v in (sig.signal_params or {}).items()}

                cur.execute("SAVEPOINT sp_signal")
                cur.execute("""
                    INSERT INTO execution_signals
                        (strategy_id, workspace_id, signal_date, ticker, direction,
                         entry_price, stop_loss, target_1, target_2, target_3,
                         position_size_pct, regime_state, signal_params, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
                    ON CONFLICT (strategy_id, signal_date, ticker, direction) DO NOTHING
                """, (
                    strategy_id, WORKSPACE, run_date,
                    sig.ticker, sig.direction,
                    sig.entry_price, sig.stop_loss,
                    sig.target_1, sig.target_2, sig.target_3,
                    sig.position_size_pct, regime_state,
                    json.dumps(params_clean),
                ))
                rows_inserted = max(cur.rowcount, 0)  # ON CONFLICT DO NOTHING returns -1
                cur.execute("RELEASE SAVEPOINT sp_signal")
                total += rows_inserted
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_signal")
                cur.execute("RELEASE SAVEPOINT sp_signal")
                logger.error(f"write_signals error for {strategy_id}/{sig.ticker}: {e}")
    return total


# ──────────────────────────────────────────────────────────
# 6. CONFLUENCE
# ──────────────────────────────────────────────────────────

def detect_confluence(cur, strategy_results: dict, regime_state: str, run_date: date) -> int:
    """Identify tickers where ≥2 strategies agree on direction."""
    # Build ticker → {direction → [strategy_ids]}
    agree: dict = {}
    for strat_id, signals in strategy_results.items():
        for sig in signals:
            key = (sig.ticker, sig.direction)
            agree.setdefault(key, []).append(strat_id)

    count = 0
    for (ticker, direction), strats in agree.items():
        if len(strats) < CONFLUENCE_MIN:
            continue

        # Sum position sizes for combined sizing
        all_sigs = [s for sid in strats for s in strategy_results[sid]
                    if s.ticker == ticker and s.direction == direction]
        combined = sum(s.position_size_pct for s in all_sigs)

        try:
            cur.execute("""
                INSERT INTO confluence_signals
                    (workspace_id, signal_date, ticker, direction,
                     agreeing_strategies, confluence_count, regime_state, combined_size_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_id, signal_date, ticker, direction) DO NOTHING
            """, (
                WORKSPACE, run_date, ticker, direction,
                strats, len(strats), regime_state, round(combined, 4),
            ))
            count += cur.rowcount
        except Exception as e:
            logger.error(f"detect_confluence error {ticker}: {e}")

    return count


# ──────────────────────────────────────────────────────────
# 7. UPDATE P&L
# ──────────────────────────────────────────────────────────

def update_pnl(cur, prices: pd.DataFrame, run_date: date) -> int:
    """Update unrealized P&L for all open signals. Close if stop/target hit."""
    cur.execute("""
        SELECT id, strategy_id, ticker, direction, entry_price,
               stop_loss, target_1, signal_date
        FROM execution_signals
        WHERE workspace_id = %s AND status = 'open'
    """, (WORKSPACE,))
    open_signals = cur.fetchall()

    updates = 0
    for row in open_signals:
        sig_id     = row['id']
        strat_id   = row['strategy_id']
        ticker     = row['ticker']
        direction  = row['direction']
        entry      = float(row['entry_price'])
        stop_loss  = float(row['stop_loss'])
        target_1   = float(row['target_1'])
        sig_date   = row['signal_date']

        if ticker not in prices.columns:
            continue

        ts = prices[ticker].dropna()
        if ts.empty:
            continue

        current = float(ts.iloc[-1])
        days_held = (run_date - sig_date).days if isinstance(sig_date, date) else 0

        # Compute unrealized P&L
        if direction == 'LONG':
            unrealized_pct = (current - entry) / entry
        elif direction == 'SHORT':
            unrealized_pct = (entry - current) / entry
        else:  # SELL_VOL, BUY_VOL, FLAT — mark as neutral
            unrealized_pct = 0.0

        # Determine if signal should close
        close_reason = None
        close_status = 'open'
        realized_pct = None

        if direction == 'LONG' and current <= stop_loss * (1 + STOP_TRIGGER_PCT):
            close_reason = 'stop_loss'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'SHORT' and current >= stop_loss * (1 - STOP_TRIGGER_PCT):
            close_reason = 'stop_loss'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'LONG' and current >= target_1 * (1 - TARGET1_TRIGGER_PCT):
            close_reason = 'target_1'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'SHORT' and current <= target_1 * (1 + TARGET1_TRIGGER_PCT):
            close_reason = 'target_1'
            close_status = 'closed'
            realized_pct = unrealized_pct

        try:
            # Upsert P&L row
            cur.execute("""
                INSERT INTO signal_pnl
                    (signal_id, strategy_id, workspace_id, pnl_date,
                     close_price, unrealized_pnl_pct, days_held, status,
                     closed_price, closed_at, close_reason, realized_pnl_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id, pnl_date) DO UPDATE SET
                    close_price       = EXCLUDED.close_price,
                    unrealized_pnl_pct= EXCLUDED.unrealized_pnl_pct,
                    days_held         = EXCLUDED.days_held,
                    status            = EXCLUDED.status,
                    closed_price      = EXCLUDED.closed_price,
                    closed_at         = EXCLUDED.closed_at,
                    close_reason      = EXCLUDED.close_reason,
                    realized_pnl_pct  = EXCLUDED.realized_pnl_pct
            """, (
                sig_id, strat_id, WORKSPACE, run_date,
                current, round(unrealized_pct, 6), days_held,
                close_status,
                current if close_status == 'closed' else None,
                run_date if close_status == 'closed' else None,
                close_reason,
                round(realized_pct, 6) if realized_pct is not None else None,
            ))

            if close_status == 'closed':
                cur.execute(
                    "UPDATE execution_signals SET status='closed' WHERE id=%s",
                    (sig_id,)
                )

            updates += 1
        except Exception as e:
            logger.error(f"update_pnl error {sig_id}: {e}")

    return updates


# ──────────────────────────────────────────────────────────
# 8. REPORT TRIGGERS
# ──────────────────────────────────────────────────────────

def fire_report_triggers(cur, prices: pd.DataFrame, run_date: date) -> int:
    """Queue report triggers for significant P&L events."""
    cur.execute("""
        SELECT sp.signal_id, sp.strategy_id, sp.unrealized_pnl_pct,
               sp.days_held, sp.close_reason, es.ticker, es.direction
        FROM signal_pnl sp
        JOIN execution_signals es ON es.id = sp.signal_id
        WHERE sp.pnl_date = %s AND sp.workspace_id = %s
    """, (run_date, WORKSPACE))

    fired = 0
    for row in cur.fetchall():
        trigger_type   = None
        trigger_reason = None

        if row['close_reason'] == 'stop_loss':
            trigger_type   = 'STOP_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} stopped out at {row['unrealized_pnl_pct']:.1%}"
        elif row['close_reason'] == 'target_1':
            trigger_type   = 'TARGET_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} hit T1 at {row['unrealized_pnl_pct']:.1%}"
        elif (row['unrealized_pnl_pct'] or 0) < DRAWDOWN_REPORT_PCT:
            trigger_type   = 'DRAWDOWN'
            trigger_reason = f"{row['ticker']} {row['direction']} drawdown {row['unrealized_pnl_pct']:.1%}"
        elif (row['days_held'] or 0) >= DAYS_HELD_REPORT:
            trigger_type   = 'AGED'
            trigger_reason = f"{row['ticker']} {row['direction']} held {row['days_held']} days — review"

        if trigger_type:
            try:
                cur.execute("""
                    INSERT INTO report_triggers
                        (strategy_id, workspace_id, trigger_type, trigger_reason)
                    VALUES (%s,%s,%s,%s)
                """, (row['strategy_id'], WORKSPACE, trigger_type, trigger_reason))
                fired += 1
            except Exception as e:
                logger.error(f"fire_report_triggers error: {e}")

    return fired


# ──────────────────────────────────────────────────────────
# 9. LOG EXECUTION RUN
# ──────────────────────────────────────────────────────────

def log_run(cur, run_date, regime_state, metrics: dict):
    cur.execute("""
        INSERT INTO execution_runs
            (workspace_id, run_date, regime_state, strategies_run,
             signals_generated, high_confluence_signals, pnl_updates,
             report_triggers_fired, duration_seconds, errors)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        WORKSPACE, run_date, regime_state,
        metrics.get('strategies_run', 0),
        metrics.get('signals_generated', 0),
        metrics.get('confluence_count', 0),
        metrics.get('pnl_updates', 0),
        metrics.get('report_triggers', 0),
        metrics.get('duration_s', 0),
        json.dumps(metrics.get('errors', [])),
    ))


# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────

def main():
    import time
    t0       = time.time()
    run_date = date.today()
    errors   = []

    logger.info(f"=== Execution Engine START {run_date} ===")

    if not DB_URI:
        logger.error("POSTGRES_URI not set — aborting")
        sys.exit(1)

    conn = get_db()
    conn.autocommit = False
    cur  = conn.cursor()

    # Resolve workspace name → UUID once at startup
    global WORKSPACE
    WORKSPACE = resolve_workspace(cur, WORKSPACE)
    logger.info(f"Workspace: {WORKSPACE}")

    try:
        # 1. Regime
        regime = load_regime(cur)
        regime_state = regime['state']
        logger.info(f"Regime: {regime_state} (VIX={regime.get('vix_level')})")

        # 2. Strategies
        strategies = load_approved_strategies(cur)
        if not strategies:
            logger.info("No approved strategies — nothing to do")
            log_run(cur, run_date, regime_state, {'strategies_run': 0, 'errors': []})
            conn.commit()
            return

        # Build combined universe
        universe = []
        for s in strategies:
            # Strategy universe comes from DB row; fallback to SP100 proxy
            universe.extend(getattr(s, '_universe', []))
        if not universe:
            # Build universe from active tickers in master prices parquet
            prices_path = ROOT / 'data' / 'master' / 'prices.parquet'
            if prices_path.exists():
                try:
                    import pyarrow.parquet as pq
                    tickers = pq.read_table(prices_path, columns=['ticker']).to_pandas()['ticker'].unique().tolist()
                    universe = sorted(tickers)
                    logger.info(f"Universe from master prices: {len(universe)} tickers")
                except Exception:
                    universe = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'AMZN', 'NVDA', 'GOOGL', 'META']
            else:
                universe = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'AMZN', 'NVDA', 'GOOGL', 'META']
        universe = list(dict.fromkeys(universe))  # dedupe preserving order

        # 3. Load data
        prices   = load_prices(universe)
        aux_data = load_aux_data(universe)

        if prices.empty:
            logger.warning("Prices DataFrame empty — signals will be minimal")

        # 4. Run strategies
        strategy_results = run_strategies(strategies, prices, regime, universe, aux_data)

        # 5. Write signals
        total_signals = write_signals(cur, strategy_results, regime_state, run_date)
        logger.info(f"Signals written: {total_signals}")

        # 6. Confluence
        confluence_count = detect_confluence(cur, strategy_results, regime_state, run_date)
        logger.info(f"Confluence signals: {confluence_count}")

        # 7. P&L updates
        pnl_updates = update_pnl(cur, prices, run_date)
        logger.info(f"P&L rows updated: {pnl_updates}")

        # 8. Report triggers
        report_triggers = fire_report_triggers(cur, prices, run_date)
        logger.info(f"Report triggers fired: {report_triggers}")

        duration_s = round(time.time() - t0, 2)

        # 9. Log run
        log_run(cur, run_date, regime_state, {
            'strategies_run':    len(strategies),
            'signals_generated': total_signals,
            'confluence_count':  confluence_count,
            'pnl_updates':       pnl_updates,
            'report_triggers':   report_triggers,
            'duration_s':        duration_s,
            'errors':            errors,
        })

        conn.commit()
        logger.info(f"=== Execution Engine DONE in {duration_s}s ===")

        # Output JSON for caller
        print(json.dumps({
            'status':            'ok',
            'run_date':          str(run_date),
            'regime':            regime_state,
            'strategies_run':    len(strategies),
            'signals_generated': total_signals,
            'confluence_count':  confluence_count,
            'pnl_updates':       pnl_updates,
            'report_triggers':   report_triggers,
            'duration_s':        duration_s,
        }))

    except Exception as e:
        conn.rollback()
        logger.error(f"FATAL: {e}\n{traceback.format_exc()}")
        errors.append(str(e))
        try:
            log_run(cur, run_date, 'UNKNOWN', {'errors': errors})
            conn.commit()
        except Exception:
            pass
        print(json.dumps({'status': 'error', 'error': str(e)}))
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
