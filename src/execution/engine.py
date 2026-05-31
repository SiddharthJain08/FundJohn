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
import decimal
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pandas as pd
import numpy as np

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from strategies.registry import get_approved_strategies
from strategies.regime_gate import is_eligible
from strategies.instrument_class import instrument_class_for
from regime.crypto_regime import load_crypto_regime_state
from execution import regime_param_override

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


_ALPACA_BIN = '/root/go/bin/alpaca'


def _next_trading_day(run_date: date) -> date:
    """Derive the next trading session date after run_date, respecting US market holidays.

    Uses the Alpaca market calendar CLI (holiday-aware) as the primary source.
    Queries --start run_date+1 --end run_date+7 and returns the first calendar
    session date.  Falls back to plain weekday-skip math ONLY if the CLI call
    fails, and logs a warning in that case.

    The holiday-aware approach is required because the next-session reconcile
    (Task 3 onwards) queries `target_date = today`.  If target_date were set to
    a market holiday, the signal would be silently orphaned.

    Example: run_date=2026-07-02 (Thursday before observed July 4)
      - weekday math → 2026-07-03 (Friday) — WRONG; market is closed
      - calendar     → 2026-07-06 (Monday) — correct next session

    Args:
        run_date: The current EOD run date.

    Returns:
        The next trading session date after run_date.
    """
    start = run_date + timedelta(days=1)
    end = run_date + timedelta(days=7)
    try:
        result = subprocess.run(
            [_ALPACA_BIN, 'calendar',
             '--start', start.isoformat(),
             '--end', end.isoformat()],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning(
                '_next_trading_day: alpaca calendar call failed (rc=%s); '
                'falling back to weekday-skip math for run_date=%s',
                result.returncode, run_date,
            )
        else:
            sessions = json.loads(result.stdout) if result.stdout.strip() else []
            if sessions:
                return date.fromisoformat(sessions[0]['date'])
            logger.warning(
                '_next_trading_day: alpaca calendar returned empty session list; '
                'falling back to weekday-skip math for run_date=%s',
                run_date,
            )
    except Exception as exc:
        logger.warning(
            '_next_trading_day: alpaca calendar exception (%s); '
            'falling back to weekday-skip math for run_date=%s',
            exc, run_date,
        )
    # Fallback: skip weekends only (no holiday awareness)
    d = start
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def _eod_signal_register_gate_on() -> bool:
    """Returns True if OPENCLAW_EOD_SIGNAL_REGISTER==1 (default OFF)."""
    return os.environ.get('OPENCLAW_EOD_SIGNAL_REGISTER') == '1'


def get_db():
    return psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.DictCursor)


# ──────────────────────────────────────────────────────────
# SP-1: options EOD parquet helpers
# ──────────────────────────────────────────────────────────

def _drop_zero_greeks(df: 'pd.DataFrame') -> 'pd.DataFrame':
    """Drop rows where ALL four greeks are zero (degenerate contracts).

    options_eod.parquet is kept as an immutable record by the archive job
    (Task 6/alpaca_options.py) — it intentionally preserves zero-greek rows
    so that the parquet reflects the raw chain snapshot (0-DTE expired
    contracts, deep-ITM no-flow rows, etc.).  Consumers that aggregate the
    chain (like this engine) must filter them out before aggregation to avoid
    contaminating IV-rank, ATM-greek, and put/call ratio calculations.

    This is the SECOND line of defence:
      1st — alpaca_options.js (Task 13) drops zero-greek rows during the live
            intraday snapshot before they ever reach the signal stage.
      2nd — this function, applied immediately after pd.read_parquet(), for
            rows that may exist in the historical archive.
    """
    if df.empty:
        return df
    greek_cols = ('delta', 'gamma', 'theta', 'vega')
    present = [c for c in greek_cols if c in df.columns]
    if not present:
        # No greek columns at all — nothing to filter on; return unchanged.
        return df
    zero_mask = pd.Series(True, index=df.index)
    for col in present:
        zero_mask = zero_mask & (df[col].fillna(0) == 0)
    # Columns that are absent are treated as 0 (conservative: only drop if
    # ALL present columns are also zero).
    dropped = int(zero_mask.sum())
    if dropped > 0:
        logger.info('sp1.engine.options_filter dropped=%d degenerate rows (of %d total)', dropped, len(df))
    return df[~zero_mask].copy()


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


REGIME_LATEST_FILE = ROOT / '.agents' / 'market-state' / 'regime_latest.json'


def load_regime(cur=None) -> dict:
    """Load the latest regime from `regime_latest.json` (file-primary).

    File is the producer's single atomic write; the DB row is an
    append-only history copy now (2026-04-29). Reading the file
    eliminates the file/DB drift class of bug entirely — the
    2026-04-28 LOW_VOL miss specifically came from the DB write
    silently failing while the file stayed fresh, and the engine
    consuming the stale DB.

    Hard fail (exit 2 via SystemExit) when the file is older than
    ENGINE_REGIME_FAIL_HOURS or missing entirely — the orchestrator's
    CycleAbort path then pages the operator instead of silently
    producing TRANSITIONING signals on a LOW_VOL day. Override with
    OPENCLAW_ALLOW_STALE_REGIME=1 only for offline backtest runs.

    `cur` arg is kept for backwards-compat (existing callers pass a
    Postgres cursor) but is unused.
    """
    import os, json
    from datetime import datetime, timezone

    fail_hours  = float(os.environ.get('ENGINE_REGIME_FAIL_HOURS', '80'))
    allow_stale = os.environ.get('OPENCLAW_ALLOW_STALE_REGIME') == '1'

    if not REGIME_LATEST_FILE.exists():
        msg = (f'regime_latest.json missing at {REGIME_LATEST_FILE}. '
               f'Run scripts/run_market_state.py first.')
        logger.error(f'[engine] {msg}')
        print(f'[engine] FATAL: {msg}', flush=True)
        import sys; sys.exit(2)

    mtime = datetime.fromtimestamp(REGIME_LATEST_FILE.stat().st_mtime, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
    if age_h > fail_hours and not allow_stale:
        msg = (f'regime_latest.json is stale: mtime={mtime.isoformat()} '
               f'({age_h:.1f}h ago, limit={fail_hours}h). Refusing to '
               f'generate signals. Run scripts/run_market_state.py or set '
               f'OPENCLAW_ALLOW_STALE_REGIME=1 for backtests.')
        logger.error(f'[engine] {msg}')
        print(f'[engine] FATAL: {msg}', flush=True)
        import sys; sys.exit(2)

    j = json.loads(REGIME_LATEST_FILE.read_text())
    state    = j.get('state') or 'HIGH_VOL'
    vix      = float(j.get('vix_level') or (j.get('features') or {}).get('vix') or 25.0)
    vix_pct  = float(j.get('vix_percentile') or 50.0)
    return {
        'state':           state,
        'vix_level':       vix,
        'vix_percentile':  vix_pct,
        'regime_data':     j,                # whole file as regime_data
        'updated_at':      mtime.isoformat(),
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
    """Load master price parquet; pivot to wide format (date index × ticker columns, close prices).

    When OPENCLAW_CLOSE_PROXY_SNAPSHOT=1, append a today-dated (ET) row from a
    live close[t]-proxy snapshot so live signal generation mirrors the backtests'
    close[t] decision. The proxy fetch is deliberately OUTSIDE the parquet
    try/except: a CloseProxyError MUST propagate (abort the signals step), never
    be swallowed into an empty frame that would orphan-close the whole book.
    """
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
    except (OSError, ValueError, KeyError) as e:   # narrow: parquet read/pivot only
        logger.error(f"Failed to load prices: {e}")
        return pd.DataFrame()

    # close[t]-proxy injection — OUTSIDE the try so CloseProxyError propagates.
    if os.environ.get('OPENCLAW_CLOSE_PROXY_SNAPSHOT') == '1':
        from ingestion.close_proxy_snapshot import fetch_close_proxy
        today = pd.Timestamp.now(tz='America/New_York').normalize().tz_localize(None)
        proxy = fetch_close_proxy(list(wide.columns), today)
        if today not in wide.index:
            wide.loc[today] = pd.Series(proxy).reindex(wide.columns)
            wide.sort_index(inplace=True)

    logger.info(f"Prices loaded: {wide.shape[1]} tickers × {wide.shape[0]} dates")
    return wide


_SENTIMENT_COLS = ['ticker', 'date', 'alpaca_news_count_24h', 'alpaca_news_mean_score',
                   'alpaca_news_finbert_pos', 'alpaca_news_finbert_neu', 'alpaca_news_finbert_neg']


def _sentiment_slice(universe: list, as_of=None) -> dict:
    """Live per-ticker news sentiment for aux_data['sentiment'].

    Reads ticker_sentiment_daily.alpaca_news_* — the live home of Alpaca-news FinBERT
    scores — and remaps to the news_* keys S_news_sentiment_long_short expects, applying
    the same point-in-time forward-fill + 7-day staleness cap the backtest aux loader uses.
    (The legacy live news_* columns are a dead RSS source, ~0% covered since 2026-05-22;
    backtest parity is news_* ↔ alpaca_news_*. See src/execution/sentiment_aux.py.)

    Fail-open: any DB error → {} (sentiment feeds one strategy, not the whole cycle).
    """
    if not universe:
        return {}
    try:
        from execution.sentiment_aux import build_sentiment_aux, SENTIMENT_MAX_AGE_DAYS
        from datetime import date as _date
        if as_of is None:
            as_of = _date.today()
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {', '.join(_SENTIMENT_COLS)}
                      FROM ticker_sentiment_daily
                     WHERE ticker = ANY(%s)
                       AND date BETWEEN (%s::date - %s) AND %s::date
                       AND alpaca_news_count_24h > 0
                    """,
                    (list(universe), as_of, SENTIMENT_MAX_AGE_DAYS, as_of),
                )
                rows = [dict(zip(_SENTIMENT_COLS, r)) for r in cur.fetchall()]
        finally:
            conn.close()
        return build_sentiment_aux(rows, as_of=as_of, max_age_days=SENTIMENT_MAX_AGE_DAYS)
    except Exception as e:
        logger.warning(f"Could not load sentiment: {e}")
        return {}


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
            # Pre-compute HV20 per ticker from master prices (options_eod has no hv20 column)
            # Used for rv_20 and vrp fields consumed by S_HV9, S_HV11, S_HV12, S_HV14, S_HV20.
            _hv20_by_ticker = {}
            _hv20_history_by_ticker = {}
            try:
                _px_path = master_dir / 'prices.parquet'
                if _px_path.exists():
                    _px = pd.read_parquet(_px_path, columns=['ticker', 'date', 'close'])
                    _px['date'] = pd.to_datetime(_px['date'])
                    _px = _px.sort_values(['ticker', 'date'])
                    for _t, _g in _px.groupby('ticker'):
                        if _t not in universe or len(_g) < 22:
                            continue
                        _rets = _g['close'].pct_change()
                        # Current HV20 (annualized, fraction)
                        _hv20_now = float(_rets.iloc[-20:].std() * (252 ** 0.5))
                        if _hv20_now > 0:
                            _hv20_by_ticker[_t] = round(_hv20_now, 4)
                        # Last 8 trading days of HV20 history (rolling)
                        _hv_roll = _rets.rolling(20).std() * (252 ** 0.5)
                        _hist = [round(float(v), 4) for v in _hv_roll.dropna().tail(8).tolist()]
                        _hv20_history_by_ticker[_t] = _hist
                    logger.info(f"HV20 pre-computed: {len(_hv20_by_ticker)} tickers")
            except Exception as _e:
                logger.warning(f"HV20 pre-compute failed: {_e}")

            opts = pd.read_parquet(opts_path)
            # SP-1: drop degenerate (all-zero-greek) rows before any aggregation.
            opts = _drop_zero_greeks(opts)
            today = pd.Timestamp.today().normalize()

            # Ensure expiry is datetime
            if 'expiry' in opts.columns:
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')
            elif 'expiration_date' in opts.columns:
                opts = opts.rename(columns={'expiration_date': 'expiry'})
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')

            if 'date' in opts.columns:
                opts['date'] = pd.to_datetime(opts['date'], errors='coerce')

            # Load earnings calendar for earnings_dte (S-HV17)
            _earnings_path = Path(__file__).resolve().parent.parent.parent / 'data' / 'master' / 'earnings.parquet'
            _earnings_df   = None
            _upcoming_earnings = None
            if _earnings_path.exists():
                try:
                    _earnings_df = pd.read_parquet(_earnings_path)
                    _earnings_df['date'] = pd.to_datetime(_earnings_df['date'], errors='coerce')
                    _today_ts = pd.Timestamp.today().normalize()
                    _upcoming_earnings = _earnings_df[_earnings_df['date'] >= _today_ts].copy()
                    logger.info(f"Earnings calendar loaded: {len(_upcoming_earnings)} upcoming events")
                except Exception as _e:
                    logger.warning(f"Could not load earnings.parquet: {_e}")
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
                # rv_20: current HV20 (computed from master prices above);
                # vrp: implied vol premium over realized vol.
                rv_20 = _hv20_by_ticker.get(ticker)
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
                # hv20_history: from pre-computed price-based HV20 (options_eod has no hv20 col).
                hv20_history = _hv20_history_by_ticker.get(ticker, [])
                # vrp_history: zip IV30 history with HV20 history where both exist.
                if 'date' in grp.columns and hv20_history:
                    _iv_by_date = grp.groupby('date')['implied_volatility'].mean().dropna().sort_index()
                    _last_iv = [round(float(v), 4) for v in _iv_by_date.tail(len(hv20_history)).tolist()]
                    vrp_history = [round(iv - hv, 4) for iv, hv in zip(_last_iv, hv20_history) if iv is not None and hv is not None]
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

                
                # earnings_dte (S-HV17): days to next earnings announcement
                earnings_dte = None
                if _upcoming_earnings is not None and not _upcoming_earnings.empty:
                    _t_earn = _upcoming_earnings[_upcoming_earnings['ticker'] == ticker]
                    if not _t_earn.empty:
                        _next_earn = _t_earn['date'].min()
                        earnings_dte = int((_next_earn - _today_ts).days)

                opts_dict[ticker] = {
                    'iv_rank':                 iv_rank,
                    'iv30':                    iv30,
                    'volume':                  chain_volume,
                    'open_interest_by_strike': oi_by_strike,
                    'expiry_date':             nearest_expiry.date().isoformat(),
                    'pc_ratio':               pc_ratio,
                    'gamma_atm':              gamma_atm,
                    'theta_atm':              theta_atm,
                    'iv_spread':              iv_spread,
                    'skew_20d':               skew_20d,
                    'ts_ratio':               ts_ratio,
                    'near_iv':                near_iv_ts,
                    'far_iv':                 far_iv_ts,
                    'gex':                    gex,
                    'iv_centroid_delta':      iv_centroid_delta,
                    'surface_premium':        surface_premium,
                    'earnings_dte':           earnings_dte,
                    'rv_20':                  rv_20,
                    'vrp':                    vrp,
                    'iv_rank_history':        iv_rank_history,
                    'pc_ratio_history':       pc_ratio_history,
                    'vrp_history':            vrp_history,
                    'hv20_history':           hv20_history,
                }

            # Inject last_price from prices.parquet (long-format, always available)
            try:
                _px_path = master_dir / 'prices.parquet'
                if _px_path.exists():
                    import pandas as _pd2
                    _px = _pd2.read_parquet(_px_path, columns=['ticker','close'])
                    _lp = _px.groupby('ticker')['close'].last().to_dict()
                    for _tk, _od in opts_dict.items():
                        _od['last_price'] = _lp.get(_tk)
            except Exception as _lpe:
                logger.warning(f'last_price load failed: {_lpe}')
            aux['options'] = opts_dict
            logger.info(f"Options loaded: {len(opts_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load options: {e}")

    # 30-min intraday bars: full DataFrame (date, datetime, ticker, o/h/l/c/v/vwap)
    # Used by S-TR-04 (Zarattini) and future intraday strategies.
    bars_30m_path = master_dir / 'prices_30m.parquet'
    if bars_30m_path.exists():
        try:
            bars_30m = pd.read_parquet(bars_30m_path)
            if not bars_30m.empty:
                aux['prices_30m'] = bars_30m
                logger.info(f"30m bars loaded: {len(bars_30m):,} rows, {bars_30m['ticker'].nunique()} tickers")
        except Exception as e:
            logger.warning(f"Could not load prices_30m: {e}")

    # Macro time series: {series_name: pd.Series(date_index → value)}
    # Provides VIX, VVIX, VIX3M, etc. for regime-aware strategies (S-TR-01 etc.)
    macro_path = master_dir / 'macro.parquet'
    if macro_path.exists():
        try:
            mac = pd.read_parquet(macro_path)
            if not mac.empty and {'date', 'series', 'value'}.issubset(mac.columns):
                mac['date'] = pd.to_datetime(mac['date'])
                mac_dict: dict[str, pd.Series] = {}
                for series_name, grp in mac.groupby('series'):
                    mac_dict[series_name] = (
                        grp.set_index('date')['value']
                        .sort_index()
                        .dropna()
                    )
                aux['macro'] = mac_dict
                logger.info(f"Macro loaded: {list(mac_dict.keys())} series")
        except Exception as e:
            logger.warning(f"Could not load macro: {e}")

    # Sentiment: {ticker: {news_count_24h, news_mean_score, news_finbert_pos/neu/neg}}
    # Read from ticker_sentiment_daily.alpaca_news_* (live Alpaca-news FinBERT scores),
    # remapped to the news_* keys S_news_sentiment_long_short expects — backtest parity.
    aux['sentiment'] = _sentiment_slice(universe)
    logger.info(f"Sentiment loaded: {len(aux['sentiment'])} tickers")

    return aux


# ──────────────────────────────────────────────────────────
# 4. RUN STRATEGIES
# ──────────────────────────────────────────────────────────

def _apply_regime_overrides_to_signals(strategy_id, signals, regime_state):
    """Mutate each Signal's stop_loss/target_1 with the per-(strategy, regime)
    override (gated; no-op when OPENCLAW_BACKTEST_COUPLED_RECS is unset). Mirrors
    the backtest's simulate-time application so live and backtest agree."""
    ov = regime_param_override.resolve_override(strategy_id, str(regime_state))
    if not ov:
        return
    for sig in signals or []:
        # Classify via the shared normalizer (mirrors the backtest's
        # _signal_to_long_short): LONG/BUY/BUY_VOL → +1, SHORT/SELL/SELL_VOL → -1,
        # FLAT/unknown → 0. Skip 0 so FLAT/unknown directions get NO override —
        # the backtest skips those trades, so live must too (parity + safety:
        # never persist an inverted bracket for a non-long/short signal).
        d = regime_param_override.direction_sign(sig.direction)
        if d == 0:
            continue
        ep = float(sig.entry_price) if getattr(sig, 'entry_price', 0) else 0.0
        if ep <= 0:
            continue
        sig.stop_loss, sig.target_1 = regime_param_override.apply_override(
            entry_price=ep, direction=d,
            stop_loss=float(sig.stop_loss or 0), target_1=float(sig.target_1 or 0),
            override=ov)


def run_strategies(strategies, prices, regime, universe, aux_data) -> dict:
    """
    Returns: {strategy_id: [Signal, ...]}

    Side-effect: sets run_strategies.last_run_stats after every call:
        {'total': <len(strategies)>, 'errored': <count of exception paths>,
         'ran_ok': total - errored}
    Strategies that were skipped (regime-ineligible, no crypto regime) are NOT
    counted as errored — they didn't raise.  Callers that need to distinguish
    "ran fine but emitted zero signals" from "raised an exception" should read
    last_run_stats rather than checking `if signals` on the results dict (which
    cannot distinguish the two cases since both store []).
    """
    # regime is the full dict from load_regime(); the eligibility gate takes
    # the regime-state string. Strategies still get the full dict.
    equity_regime_str = regime['state'] if isinstance(regime, dict) else regime
    _crypto_regime = None  # lazily loaded only if a crypto strategy is present
    results = {}
    errored_ids: set = set()
    for strat in strategies:
        try:
            # SP-3.1 Phase C: crypto strategies gate on the CRYPTO regime, not equity.
            if instrument_class_for(strat.id) == 'crypto':
                if _crypto_regime is None:
                    _crypto_regime = load_crypto_regime_state()
                strat_regime = _crypto_regime
                strat_regime_str = _crypto_regime.get('state')
                if not strat_regime_str:
                    logger.info('[engine] %s skipped — no crypto regime available', strat.id)
                    continue
            else:
                strat_regime = regime
                strat_regime_str = equity_regime_str
            if not is_eligible(strat.id, strat_regime_str):
                logger.info('[engine] %s skipped — regime %s not in eligible_regimes', strat.id, strat_regime_str)
                continue
            signals = strat.generate_signals(prices, strat_regime, universe, aux_data)
            _apply_regime_overrides_to_signals(strat.id, signals, strat_regime_str)
            results[strat.id] = signals or []
            logger.info(f"  {strat.id}: {len(results[strat.id])} signals")
        except Exception as e:
            logger.error(f"  {strat.id} FAILED: {e}\n{traceback.format_exc()}")
            errored_ids.add(strat.id)
            results[strat.id] = []
    total = len(strategies)
    run_strategies.last_run_stats = {
        'total': total,
        'errored': len(errored_ids),
        'ran_ok': total - len(errored_ids),
    }
    return results


# ──────────────────────────────────────────────────────────
# 4.5. WRITE EOD COMPUTE HEALTH SENTINEL
# ──────────────────────────────────────────────────────────

def write_eod_health(cur, run_date: date, *, rc: int, n_strategies_ok: int,
                     n_strategies_total: int, regime_ok: bool, universe_size: int) -> None:
    """
    INSERT one eod_compute_health row after run_strategies.

    Gated by _eod_signal_register_gate_on(): caller is responsible for the gate
    check so this function can also be called directly in tests with any cursor.

    n_strategies_ok: strategies that executed WITHOUT raising an exception,
        regardless of whether they emitted signals.  Read from
        run_strategies.last_run_stats['ran_ok'] at the call-site.
        An all-flat successful day (every strategy ran fine, none emitted
        signals) → ran_ok == total > 0 → healthy=True → Task 8 flatten fires.
        An all-errored day → ran_ok=0 → healthy=False → flatten refused.
        (Cannot infer this from strategy_results.values() because both the
        exception path and the genuine-zero-signal path store [].)

    healthy = (rc == 0 AND regime_ok AND universe_size > 0 AND n_strategies_ok > 0)
    """
    healthy = (rc == 0 and regime_ok and universe_size > 0 and n_strategies_ok > 0)
    detail = {
        'rc': rc,
        'n_strategies_ok': n_strategies_ok,
        'n_strategies_total': n_strategies_total,
        'regime_ok': regime_ok,
        'universe_size': universe_size,
        'healthy': healthy,
    }

    cur.execute(
        """
        INSERT INTO eod_compute_health
            (run_date, rc, n_strategies_ok, n_strategies_total,
             regime_ok, universe_size, healthy, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_date, rc, n_strategies_ok, n_strategies_total,
         regime_ok, universe_size, healthy, json.dumps(detail)),
    )

    logger.info(
        "[engine] eod_compute_health: rc=%s ok=%s/%s regime_ok=%s "
        "universe_size=%s healthy=%s",
        rc, n_strategies_ok, n_strategies_total, regime_ok, universe_size, healthy,
    )


# ──────────────────────────────────────────────────────────
# 5. WRITE SIGNALS
# ──────────────────────────────────────────────────────────

def write_signals(cur, strategy_results: dict, regime_state: str, run_date: date) -> int:
    total = 0
    # Hoist gate check and next-trading-day lookup ONCE per call (not per signal).
    # When gate is OFF, next_td stays None — no CLI subprocess spawned.
    _gate_on = _eod_signal_register_gate_on()
    _next_td = _next_trading_day(run_date) if _gate_on else None
    for strategy_id, signals in strategy_results.items():
        for sig in signals:
            try:
                # Guard: skip signals with NaN/Inf in core price fields — they
                # corrupt the sizer and break the no_new_nan_signals_after_fix check.
                import math as _math
                _price_fields = [sig.entry_price, sig.stop_loss, sig.target_1]
                if any(v is not None and not _math.isfinite(float(v)) for v in _price_fields):
                    logger.warning(
                        f'[engine] Dropping NaN signal: {strategy_id}/{sig.ticker} '
                        f'(entry={sig.entry_price}, stop={sig.stop_loss}, t1={sig.target_1})'
                    )
                    continue
                # Guard: skip signals with degenerate geometry (stop == entry or inverted).
                # Root cause: ATR=0 from stale/ffilled constant price series.
                _ep, _sl, _t1 = float(sig.entry_price), float(sig.stop_loss), float(sig.target_1)
                _dirn = (sig.direction or '').upper()
                if _dirn in ('LONG', 'SHORT') and all(_math.isfinite(v) for v in [_ep, _sl, _t1]):
                    _bad = ((_dirn == 'LONG' and not (_t1 > _ep > _sl)) or
                            (_dirn == 'SHORT' and not (_t1 < _ep < _sl)))
                    if _bad:
                        logger.warning(
                            f'[engine] Dropping degenerate-geometry signal: {strategy_id}/{sig.ticker} '
                            f'(direction={_dirn} entry={_ep:.4f} stop={_sl:.4f} t1={_t1:.4f})'
                        )
                        continue
                # Serialize signal_params — convert numpy scalars to native Python
                # and sanitize NaN/Infinity so Postgres JSON accepts the row.
                def _to_native(v):
                    if hasattr(v, 'item'):
                        v = v.item()   # numpy scalar → Python scalar
                    if isinstance(v, float) and not _math.isfinite(v):
                        return None    # Postgres rejects NaN/Inf in jsonb
                    if isinstance(v, dict):
                        return {k: _to_native(vv) for k, vv in v.items()}
                    if isinstance(v, (list, tuple)):
                        return [_to_native(vv) for vv in v]
                    return v
                params_clean = {k: _to_native(v) for k, v in (sig.signal_params or {}).items()}
                # Signal.features (added Phase 5) — fold into signal_params under
                # a reserved 'features' key so trade_handoff_builder can pull them
                # out when computing the structured handoff. Strategies that don't
                # set features leave this absent.
                feats = getattr(sig, 'features', None)
                if feats:
                    params_clean['features'] = {k: _to_native(v) for k, v in feats.items()}

                cur.execute("SAVEPOINT sp_signal")
                # 2026-05-19 Phase A: bracket-upsert per (strategy, ticker).
                # Invariant: at most ONE open execution_signals row per
                # (strategy_id, ticker). Re-emissions don't insert a new row;
                # they UPDATE the existing one's bracket fields so the
                # strategy's current intent (stop/target/size/signal_params)
                # is reflected in DB, dashboard, and sizer reads on every
                # cycle. entry_price + signal_date stay at the original
                # entry (audit trail of when the position was first taken).
                #
                # Live broker bracket leg is NOT touched here — that's
                # Phase B (scripts/sync_brackets.py, gated by
                # OPENCLAW_DAILY_BRACKET_REPLACE=1). Without B, a held
                # position's broker stop-leg keeps its day-0 values until
                # natural exit + re-entry; the DB and dashboard always
                # show the strategy's current intent.
                cur.execute("""
                    SELECT id FROM execution_signals
                     WHERE strategy_id = %s AND ticker = %s AND status = 'open'
                     LIMIT 1
                """, (strategy_id, sig.ticker))
                existing_row = cur.fetchone()
                if existing_row:
                    cur.execute("""
                        UPDATE execution_signals
                           SET stop_loss        = %s,
                               target_1         = %s,
                               target_2         = %s,
                               target_3         = %s,
                               position_size_pct = %s,
                               regime_state     = %s,
                               signal_params    = %s::jsonb
                         WHERE id = %s
                    """, (
                        sig.stop_loss, sig.target_1, sig.target_2, sig.target_3,
                        sig.position_size_pct, regime_state,
                        json.dumps(params_clean), existing_row[0],
                    ))
                    # Post-update geometry guard: when entry_price is frozen
                    # (audit-trail invariant) but the new stop/target were
                    # computed from a different current price (e.g. intraday
                    # redeploy vs main pipeline running at different times),
                    # the update can produce stop > entry for LONG or stop <
                    # entry for SHORT.  Roll back the UPDATE in that case —
                    # the pre-existing clean geometry is safer than an inverted
                    # bracket.
                    cur.execute("""
                        SELECT entry_price, stop_loss, target_1, direction
                          FROM execution_signals WHERE id = %s
                    """, (existing_row[0],))
                    _chk = cur.fetchone()
                    if _chk:
                        _ep, _sl, _t1, _dirn = (float(_chk[0]), float(_chk[1]),
                                                 float(_chk[2]), _chk[3])
                        _bad_geo = (
                            all(_math.isfinite(v) for v in [_ep, _sl, _t1]) and (
                                (_dirn.upper() == 'LONG'  and not (_t1 > _ep > _sl)) or
                                (_dirn.upper() == 'SHORT' and not (_t1 < _ep < _sl))
                            )
                        )
                        if _bad_geo:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_signal")
                            cur.execute("RELEASE SAVEPOINT sp_signal")
                            logger.warning(
                                f'[engine] Skipping bracket-update: {strategy_id}/{sig.ticker} '
                                f'would create inverted geometry '
                                f'(entry={_ep:.4f} stop={_sl:.4f} t1={_t1:.4f})'
                            )
                            continue
                    rows_inserted = 0   # not a new emit; bracket refresh only
                else:
                    if _gate_on:
                        # Gate ON: include lifecycle_state, computed_at, target_date
                        cur.execute("""
                            INSERT INTO execution_signals
                                (strategy_id, workspace_id, signal_date, ticker, direction,
                                 entry_price, stop_loss, target_1, target_2, target_3,
                                 position_size_pct, regime_state, signal_params, status,
                                 lifecycle_state, computed_at, target_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'open',
                                    %s,%s,%s)
                            ON CONFLICT (strategy_id, signal_date, ticker, direction) DO NOTHING
                        """, (
                            strategy_id, WORKSPACE, run_date,
                            sig.ticker, sig.direction,
                            sig.entry_price, sig.stop_loss,
                            sig.target_1, sig.target_2, sig.target_3,
                            sig.position_size_pct, regime_state,
                            json.dumps(params_clean),
                            'COMPUTED', datetime.now(timezone.utc), _next_td,
                        ))
                    else:
                        # Gate OFF: legacy INSERT — byte-identical to pre-SP-6 behavior
                        cur.execute("""
                            INSERT INTO execution_signals
                                (strategy_id, workspace_id, signal_date, ticker, direction,
                                 entry_price, stop_loss, target_1, target_2, target_3,
                                 position_size_pct, regime_state, signal_params, status)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'open')
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

def update_pnl(cur, prices: pd.DataFrame, run_date: date) -> tuple[int, list]:
    """Update unrealized P&L for all open signals. Close if stop/target hit.

    Returns (n_updates, newly_closed_signal_ids). The ids are classified by
    the caller AFTER it commits — see the note at the end of this function."""
    cur.execute("""
        SELECT id, strategy_id, ticker, direction, entry_price,
               stop_loss, target_1, signal_date
        FROM execution_signals
        WHERE workspace_id = %s AND status = 'open'
    """, (WORKSPACE,))
    open_signals = cur.fetchall()

    updates = 0
    _newly_closed_signal_ids: list[int] = []
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

        # Compute unrealized P&L; guard against zero/NaN entries.
        import math as _math
        if not entry or not _math.isfinite(entry):
            unrealized_pct = 0.0
        elif direction == 'LONG':
            unrealized_pct = (current - entry) / entry
        elif direction == 'SHORT':
            unrealized_pct = (entry - current) / entry
        else:  # SELL_VOL, BUY_VOL, FLAT — mark as neutral
            unrealized_pct = 0.0
        if not _math.isfinite(unrealized_pct):
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
                # OUE classification (over/under/expected vs the signal's
                # GBM expectation captured at handoff time). Deferred to
                # after this loop so a single failed lookup doesn't roll
                # back the close itself.
                _newly_closed_signal_ids.append(sig_id)

            updates += 1
        except Exception as e:
            logger.error(f"update_pnl error {sig_id}: {e}")

    # OUE classification is intentionally NOT done here. classify_batch
    # reads on its own connection, so it must run AFTER the caller commits
    # these closes — otherwise it sees uncommitted rows, finds no realized
    # P&L, and skips every signal (the 2026-05-16→2026-05-29 bug where
    # oue_kind stayed NULL: logs showed 'skipped': N for every close).
    # Return the newly-closed ids so run() can classify them post-commit.
    return updates, _newly_closed_signal_ids


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

        # Coerce Decimal/None/NaN from the DB into a plain float once; the
        # raw Decimal('NaN') that occasionally comes back from NUMERIC
        # columns can't be compared against a Python float and raises
        # decimal.InvalidOperation.
        try:
            _pnl_pct = float(row['unrealized_pnl_pct']) if row['unrealized_pnl_pct'] is not None else 0.0
        except (TypeError, ValueError, decimal.InvalidOperation):
            _pnl_pct = 0.0
        if _pnl_pct != _pnl_pct:   # NaN check
            _pnl_pct = 0.0

        if row['close_reason'] == 'stop_loss':
            trigger_type   = 'STOP_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} stopped out at {_pnl_pct:.1%}"
        elif row['close_reason'] == 'target_1':
            trigger_type   = 'TARGET_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} hit T1 at {_pnl_pct:.1%}"
        elif _pnl_pct < DRAWDOWN_REPORT_PCT:
            trigger_type   = 'DRAWDOWN'
            trigger_reason = f"{row['ticker']} {row['direction']} drawdown {_pnl_pct:.1%}"
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

def _fatal_exit_code(exc) -> int:
    """Exit code for a fatal engine error.

    CloseProxyError (the close[t]-proxy snapshot fetch failed entirely) is a
    data-availability FATAL: return 2 so the LangGraph step node aborts the
    whole cycle regardless of strict mode. rc=1 is treated as a soft 'warn'
    that lets the chain continue to handoff/trade — on an empty signal set that
    would orphan-close the book (the 2026-05-22 failure mode). All other errors
    keep the legacy rc=1.
    """
    return 2 if type(exc).__name__ == 'CloseProxyError' else 1


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

        # Inject last_price from prices DataFrame into each ticker's opts entry
        if 'options' in aux_data and prices is not None:
            try:
                latest_px = prices.groupby('ticker')['close'].last().to_dict()
                for _tk, _opts in aux_data['options'].items():
                    _opts['last_price'] = latest_px.get(_tk)
            except Exception as _e:
                logger.warning(f'last_price inject failed: {_e}')
        strategy_results = run_strategies(strategies, prices, regime, universe, aux_data)

        # 4.5 Write eod_compute_health sentinel (gated: OPENCLAW_EOD_SIGNAL_REGISTER==1)
        if _eod_signal_register_gate_on():
            _stats = run_strategies.last_run_stats
            n_strategies_total = _stats['total']
            n_strategies_ok = _stats['ran_ok']
            regime_ok = bool(
                regime_state and
                regime_state in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
            )
            write_eod_health(
                cur,
                run_date,
                rc=0,
                n_strategies_ok=n_strategies_ok,
                n_strategies_total=n_strategies_total,
                regime_ok=regime_ok,
                universe_size=len(universe),
            )

        # 5. Write signals
        total_signals = write_signals(cur, strategy_results, regime_state, run_date)
        logger.info(f"Signals written: {total_signals}")

        # 5a. Parity marks — mark entry at close[T+1] and re-anchor brackets.
        #     Gated by OPENCLAW_EOD_SIGNAL_REGISTER==1 (same gate as write_signals).
        #     Builds closes dict from prices (wide: date-index × ticker-columns)
        #     using last non-NaN close per ticker — mirrors update_pnl's proven
        #     pattern (prices[ticker].dropna().iloc[-1]).
        parity_mark_count = 0
        if _eod_signal_register_gate_on():
            try:
                from execution.parity_mark import finalize_parity_marks
                _closes: dict = {}
                if not prices.empty:
                    for _tk in prices.columns:
                        _ts = prices[_tk].dropna()
                        if not _ts.empty:
                            _closes[_tk] = float(_ts.iloc[-1])
                parity_mark_count = finalize_parity_marks(cur, _closes, run_date, WORKSPACE)
                logger.info(f"Parity marks finalized: {parity_mark_count}")
            except Exception as _pm_err:
                logger.error(f"[engine] parity_mark failed: {_pm_err}")
                errors.append(f"parity_mark: {_pm_err}")

        # 6. Confluence
        confluence_count = detect_confluence(cur, strategy_results, regime_state, run_date)
        logger.info(f"Confluence signals: {confluence_count}")

        # 7. P&L updates
        pnl_updates, newly_closed_ids = update_pnl(cur, prices, run_date)
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

        # OUE classification — MUST be after the commit above so the
        # classifier's own connection can see the just-closed signal_pnl
        # rows. Best-effort: failures here never fail the cycle (closes are
        # already durable); a later run/backfill picks up any stragglers
        # since classify_batch skips already-classified signals.
        if newly_closed_ids:
            try:
                from execution.oue_classifier import classify_batch
                uri = os.environ.get('POSTGRES_URI', '')
                if uri:
                    stats = classify_batch(uri, newly_closed_ids)
                    logger.info(f"OUE classified {len(newly_closed_ids)} closes: {stats}")
            except Exception as e:
                logger.warning(f"OUE classify_batch failed (closes still persisted): {e}")

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
        sys.exit(_fatal_exit_code(e))
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
