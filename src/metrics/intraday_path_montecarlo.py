#!/usr/bin/env python3
"""Path-dependent Monte Carlo for size/stop/target/max-hold proposals.

Phase 2E. Counterpart to `regime_param_montecarlo.py` (linear-scaling MC,
Phase 2D) — instead of resampling realized PnLs and multiplying by a size
ratio, this simulates intraday return paths and applies the proposed
policy (stop_pct, target_pct, max_hold_days) bar-by-bar to derive realized
PnL.

Two path generators:
  - EmpiricalPathGen: resamples real 30m bar return sequences from
    data/master/prices_30m.parquet (only 5 tickers as of 2026-05-13:
    AAPL, MSFT, NVDA, SPY, TSLA).
  - GBMPathGen: synthesizes geometric-Brownian-motion intraday paths
    calibrated to the ticker's daily realized vol from prices.parquet.

Dispatch: tickers with ≥20 30m bars in the window get empirical; others
get GBM. A `path_source` field on the persisted run records which
generator (or 'hybrid' if a strategy's trade pool mixes both) was used.

Spec: docs/superpowers/specs/2026-05-13-regime-blended-sizer-phase-2e-design.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path
from typing import Optional

logger = __import__('logging').getLogger(__name__)

BARS_PER_RTH_DAY = 13              # 9:30-16:00 ET / 30 min = 13 bars
MIN_TRADES_FOR_MC = 10
DEFAULT_N_ITER = 1000
PERCENTILES = (5, 50, 95)
TRADING_DAYS_PER_YEAR = 252
EMPIRICAL_BAR_FLOOR = 20           # min 30m bars to use empirical gen

_PRICES_30M_PATH = Path(os.environ.get('OPENCLAW_PRICES_30M',
                                        '/root/openclaw/data/master/prices_30m.parquet'))
_PRICES_DAILY_PATH = Path(os.environ.get('OPENCLAW_PRICES_DAILY',
                                          '/root/openclaw/data/master/prices.parquet'))


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


# ---------- Path Generators ---------- #

class EmpiricalPathGen:
    """Resamples 30m return sequences from data/master/prices_30m.parquet.

    For each sample_path() call: pick a random RTH-bar start in the window,
    take n_bars consecutive log-returns from there. Wraps around within
    the available range when n_bars exceeds remaining bars.
    """
    def __init__(self, returns: list[float]):
        self._returns = returns                  # log-returns, 30m intervals

    @classmethod
    def from_parquet(cls, ticker: str, window_days: int = 90) -> Optional['EmpiricalPathGen']:
        import pandas as pd
        if not _PRICES_30M_PATH.exists():
            return None
        df = pd.read_parquet(_PRICES_30M_PATH, columns=['ticker', 'datetime', 'close'])
        df = df[df.ticker == ticker]
        if df.empty:
            return None
        cutoff = df.datetime.max() - pd.Timedelta(days=window_days)
        df = df[df.datetime >= cutoff].sort_values('datetime').reset_index(drop=True)
        if len(df) < EMPIRICAL_BAR_FLOOR:
            return None
        closes = df['close'].astype(float).tolist()
        rets: list[float] = []
        for i in range(1, len(closes)):
            prev, cur = closes[i - 1], closes[i]
            if not (math.isfinite(prev) and math.isfinite(cur)) or prev <= 0 or cur <= 0:
                continue
            r = math.log(cur / prev)
            if math.isfinite(r):
                rets.append(r)
        if len(rets) < EMPIRICAL_BAR_FLOOR - 1:
            return None
        return cls(rets)

    def sample_path(self, n_bars: int, rng: random.Random) -> list[float]:
        if not self._returns:
            return [0.0] * n_bars
        start = rng.randrange(len(self._returns))
        path: list[float] = []
        i = start
        for _ in range(n_bars):
            path.append(self._returns[i])
            i = (i + 1) % len(self._returns)
        return path


class GBMPathGen:
    """GBM intraday paths calibrated to ticker daily realized vol.

    σ_intraday = σ_daily / sqrt(BARS_PER_RTH_DAY)
    μ_intraday = μ_daily / BARS_PER_RTH_DAY
    """
    def __init__(self, mu_intraday: float, sigma_intraday: float):
        self.mu = mu_intraday
        self.sigma = sigma_intraday

    @classmethod
    def from_parquet(cls, ticker: str, window_days: int = 90) -> Optional['GBMPathGen']:
        import pandas as pd
        if not _PRICES_DAILY_PATH.exists():
            return None
        df = pd.read_parquet(_PRICES_DAILY_PATH, columns=['ticker', 'date', 'close'])
        df = df[df.ticker == ticker]
        if df.empty:
            return None
        df['date'] = pd.to_datetime(df.date)
        cutoff = df.date.max() - pd.Timedelta(days=window_days)
        df = df[df.date >= cutoff].sort_values('date').reset_index(drop=True)
        if len(df) < 10:
            return None
        closes = df['close'].astype(float).tolist()
        daily_rets: list[float] = []
        for i in range(1, len(closes)):
            prev, cur = closes[i - 1], closes[i]
            if not (math.isfinite(prev) and math.isfinite(cur)) or prev <= 0 or cur <= 0:
                continue
            r = math.log(cur / prev)
            if math.isfinite(r):
                daily_rets.append(r)
        if len(daily_rets) < 5:
            return None
        mu_d = sum(daily_rets) / len(daily_rets)
        var_d = sum((r - mu_d) ** 2 for r in daily_rets) / max(len(daily_rets) - 1, 1)
        sigma_d = math.sqrt(var_d)
        if not (math.isfinite(mu_d) and math.isfinite(sigma_d)):
            return None
        return cls(mu_d / BARS_PER_RTH_DAY,
                    sigma_d / math.sqrt(BARS_PER_RTH_DAY))

    def sample_path(self, n_bars: int, rng: random.Random) -> list[float]:
        return [rng.gauss(self.mu, self.sigma) for _ in range(n_bars)]


_GEN_CACHE: dict[str, object] = {}


def _gen_for_ticker(ticker: str, window_days: int = 90):
    """Returns (generator, source_tag). Empirical if available else GBM
    else None."""
    cache_key = f"{ticker}:{window_days}"
    if cache_key in _GEN_CACHE:
        cached = _GEN_CACHE[cache_key]
        return cached if cached is not None else (None, None)
    emp = EmpiricalPathGen.from_parquet(ticker, window_days)
    if emp is not None:
        out = (emp, 'empirical')
        _GEN_CACHE[cache_key] = out
        return out
    gbm = GBMPathGen.from_parquet(ticker, window_days)
    if gbm is not None:
        out = (gbm, 'gbm')
        _GEN_CACHE[cache_key] = out
        return out
    _GEN_CACHE[cache_key] = None
    return (None, None)


# ---------- Policy application ---------- #

def apply_policy(path: list[float], stop_pct: float,
                  target_pct: float, max_hold_bars: int,
                  direction: str = 'LONG'
                  ) -> tuple[float, str, float]:
    """Walk a path of log-returns and apply the (stop, target, max_hold)
    policy.

    Returns (realized_log_return, exit_reason, intra_max_dd_pct).
      exit_reason ∈ {'stop', 'target', 'max_hold'}
      intra_max_dd_pct: worst peak-to-trough on the realized path,
                       expressed as fraction (negative).

    stop_pct/target_pct are absolute decimal fractions of entry price
    (e.g. 0.02 = 2% stop). For SHORT, signs flip.
    """
    if not path:
        return 0.0, 'max_hold', 0.0
    sign = 1.0 if direction.upper() == 'LONG' else -1.0
    cum_log = 0.0
    peak = 0.0
    max_dd = 0.0
    for i, r in enumerate(path[:max_hold_bars]):
        cum_log += r
        # Convert to signed return-from-entry
        ret_pct = sign * (math.exp(cum_log) - 1.0)
        # Track DD on the path
        if ret_pct > peak:
            peak = ret_pct
        dd = ret_pct - peak
        if dd < max_dd:
            max_dd = dd
        # Stop / target check
        if stop_pct is not None and ret_pct <= -abs(float(stop_pct)):
            return sign * -abs(float(stop_pct)) / sign, 'stop', max_dd
        if target_pct is not None and ret_pct >= abs(float(target_pct)):
            return sign * abs(float(target_pct)) / sign, 'target', max_dd
    # Time-out at max_hold
    final_pct = sign * (math.exp(cum_log) - 1.0)
    return final_pct, 'max_hold', max_dd


# ---------- MC engine ---------- #

def _percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        return float('nan')
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k); c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _load_trade_pool(strategy_id: str, regime_state: str,
                      window_days: int = 365) -> list[dict]:
    """Load (ticker, direction) trade pool. Resampling samples from this
    pool to pick which ticker each iteration draws a path from."""
    sql = """
        SELECT es.ticker, es.direction
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.strategy_id = %s
           AND es.regime_state = %s
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at IS NOT NULL
           AND sp.closed_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (strategy_id, regime_state, window_days))
            return [{'ticker': r[0], 'direction': r[1] or 'LONG'} for r in cur.fetchall()]


def run_path_mc(strategy_id: str, regime_state: str,
                 current_size: float, proposed_size: float,
                 proposed_stop_pct: float,
                 proposed_target_pct: float,
                 proposed_max_hold_days: int,
                 n_iter: int = DEFAULT_N_ITER,
                 window_days: int = 365,
                 seed: Optional[int] = None,
                 trade_pool: Optional[list[dict]] = None) -> dict:
    """Run path-dependent MC. Returns dict matching migration 085 columns."""
    if trade_pool is None:
        trade_pool = _load_trade_pool(strategy_id, regime_state, window_days)
    n_trades = len(trade_pool)
    if n_trades < MIN_TRADES_FOR_MC:
        return {
            'status':           'INSUFFICIENT',
            'strategy_id':      strategy_id,
            'regime_state':     regime_state,
            'n_trades_sampled': n_trades,
            'note':             f'need >= {MIN_TRADES_FOR_MC} trades, got {n_trades}',
        }
    if n_iter <= 0:
        return {'status': 'INSUFFICIENT', 'note': 'n_iter must be positive'}

    rng = random.Random(seed) if seed is not None else random.Random()
    max_hold_bars = max(1, int(proposed_max_hold_days) * BARS_PER_RTH_DAY)
    ratio = float(proposed_size) / max(float(current_size), 0.001)

    sources_used: set[str] = set()
    returns: list[float] = []
    exit_counts = {'stop': 0, 'target': 0, 'max_hold': 0, 'no_gen': 0}
    max_dds: list[float] = []
    for _ in range(n_iter):
        trade = rng.choice(trade_pool)
        gen, source = _gen_for_ticker(trade['ticker'], window_days=min(window_days, 90))
        if gen is None:
            exit_counts['no_gen'] += 1
            continue
        sources_used.add(source)
        path = gen.sample_path(max_hold_bars, rng)
        ret, exit_reason, intra_dd = apply_policy(
            path, proposed_stop_pct, proposed_target_pct,
            max_hold_bars, direction=trade['direction'])
        scaled_ret = ret * ratio
        scaled_dd = intra_dd * ratio
        if not (math.isfinite(scaled_ret) and math.isfinite(scaled_dd)):
            exit_counts['no_gen'] += 1
            continue
        returns.append(scaled_ret)
        max_dds.append(scaled_dd)
        exit_counts[exit_reason] += 1

    if not returns:
        return {
            'status':           'INSUFFICIENT',
            'strategy_id':      strategy_id,
            'regime_state':     regime_state,
            'n_trades_sampled': n_trades,
            'note':             'no path generator available for any pool ticker',
        }

    # Build bootstrap distributions of Sharpe + mean-PnL by resampling
    # the realized-return list with replacement (mirrors Phase 2D's
    # `bootstrap_pnls`). Each bootstrap is one synthetic "alternate
    # history" of the same N trades.
    sharpes: list[float] = []
    boot_means: list[float] = []
    rng2 = random.Random(rng.random())
    for _ in range(min(n_iter, len(returns))):
        bs = [rng2.choice(returns) for _ in range(len(returns))]
        m = sum(bs) / len(bs)
        boot_means.append(m)
        if len(bs) >= 2:
            v = sum((x - m) ** 2 for x in bs) / (len(bs) - 1)
            sd = math.sqrt(v)
            sharpes.append((m / sd) * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else 0.0)
    sharpes.sort()
    boot_means.sort()
    max_dds.sort()

    total_completed = sum(v for k, v in exit_counts.items() if k != 'no_gen')
    if total_completed == 0:
        total_completed = 1
    path_source = ('hybrid' if len(sources_used) > 1
                    else (next(iter(sources_used)) if sources_used else 'none'))

    return {
        'status':                  'OK',
        'strategy_id':             strategy_id,
        'regime_state':            regime_state,
        'current_size':            current_size,
        'proposed_size':           proposed_size,
        'proposed_stop_pct':       proposed_stop_pct,
        'proposed_target_pct':     proposed_target_pct,
        'proposed_max_hold_days':  proposed_max_hold_days,
        'n_trades_sampled':        n_trades,
        'n_bootstrap_iter':        n_iter,
        'path_source':             path_source,
        'sharpe_p05':              _percentile(sharpes, 5),
        'sharpe_p50':              _percentile(sharpes, 50),
        'sharpe_p95':              _percentile(sharpes, 95),
        'mean_pnl_p05':            _percentile(boot_means, 5),
        'mean_pnl_p50':            _percentile(boot_means, 50),
        'mean_pnl_p95':            _percentile(boot_means, 95),
        'max_dd_p05':              _percentile(max_dds, 5),
        'max_dd_p50':              _percentile(max_dds, 50),
        'max_dd_p95':              _percentile(max_dds, 95),
        'stop_hit_rate':           exit_counts['stop'] / total_completed,
        'target_hit_rate':         exit_counts['target'] / total_completed,
        'max_hold_hit_rate':       exit_counts['max_hold'] / total_completed,
        'no_gen_rate':             exit_counts['no_gen'] / n_iter,
    }


def persist_run(result: dict, proposal_id: Optional[int] = None) -> Optional[int]:
    """INSERT a completed run into strategy_regime_intraday_mc_runs.
    Returns row id, or None if status != OK."""
    if result.get('status') != 'OK':
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategy_regime_intraday_mc_runs
                    (strategy_id, regime_state, current_size, proposed_size,
                     proposed_stop_pct, proposed_target_pct, proposed_max_hold_days,
                     n_trades_sampled, n_bootstrap_iter, path_source,
                     sharpe_p05, sharpe_p50, sharpe_p95,
                     mean_pnl_p05, mean_pnl_p50, mean_pnl_p95,
                     max_dd_p05, max_dd_p50, max_dd_p95,
                     stop_hit_rate, target_hit_rate, max_hold_hit_rate,
                     proposal_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s)
                RETURNING id
            """, (result['strategy_id'], result['regime_state'],
                  result['current_size'], result['proposed_size'],
                  result['proposed_stop_pct'], result['proposed_target_pct'],
                  result['proposed_max_hold_days'],
                  result['n_trades_sampled'], result['n_bootstrap_iter'],
                  result['path_source'],
                  result['sharpe_p05'], result['sharpe_p50'], result['sharpe_p95'],
                  result['mean_pnl_p05'], result['mean_pnl_p50'], result['mean_pnl_p95'],
                  result['max_dd_p05'], result['max_dd_p50'], result['max_dd_p95'],
                  result['stop_hit_rate'], result['target_hit_rate'],
                  result['max_hold_hit_rate'], proposal_id))
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--strategy', required=True)
    p.add_argument('--regime', required=True)
    p.add_argument('--current', type=float, required=True)
    p.add_argument('--proposed', type=float, required=True)
    p.add_argument('--stop-pct', type=float, required=True)
    p.add_argument('--target-pct', type=float, required=True)
    p.add_argument('--max-hold-days', type=int, required=True)
    p.add_argument('--n-iter', type=int, default=DEFAULT_N_ITER)
    p.add_argument('--window-days', type=int, default=365)
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--proposal-id', type=int, default=None)
    p.add_argument('--no-persist', action='store_true')
    args = p.parse_args()
    result = run_path_mc(
        strategy_id=args.strategy, regime_state=args.regime,
        current_size=args.current, proposed_size=args.proposed,
        proposed_stop_pct=args.stop_pct,
        proposed_target_pct=args.target_pct,
        proposed_max_hold_days=args.max_hold_days,
        n_iter=args.n_iter, window_days=args.window_days, seed=args.seed,
    )
    if result.get('status') == 'OK' and not args.no_persist:
        row_id = persist_run(result, proposal_id=args.proposal_id)
        result['_row_id'] = row_id
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
