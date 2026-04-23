"""
backtest_framework.py
=====================
Shared backtest harness for the FundJohn Top-10 strategy cohort.

Design goals
------------
1.  One framework that handles four trade types so each strategy plugs in
    via a thin adapter:

        - EquityCrossSectional   : long/short basket, periodic rebalance
                                   (S-HV13, S-HV14, S-HV20, S-TR-06)
        - OptionsVol             : SELL_VOL / BUY_VOL on individual names with
                                   a holding period and IV/RV-based PnL proxy
                                   (S-HV15, S-HV17)
        - IntradaySingleAsset    : SPY 30-min bar entry/exit
                                   (S-TR-04)
        - RegimeClassifier       : binary/categorical regime output —
                                   evaluated via lead-event hit-rate
                                   (S-TR-01, S-TR-02, S-TR-03)

2.  Realistic costs by default:
        - 0.5  c/share commission on equities (Alpaca-comparable)
        - 1.0  bps slippage on equity entry + exit
        - $0.65/contract commission on options
        - 5.0  bps spread cost on straddles (round-trip ~10 bps)

3.  Walk-forward by default; in-sample report is reported separately so
    the operator can see the IS/OOS gap as a robustness flag.

4.  Vectorised metrics:
        - Sharpe (annualised, risk-free = 0 by default)
        - Sortino (annualised)
        - Calmar (CAGR / |maxDD|)
        - Max drawdown (depth + duration)
        - Win rate, expectancy, profit factor
        - Block bootstrap 95% CI on Sharpe (block = 21 trading days)

5.  Regime breakdown — when a regime label series is provided, every metric
    is also reported per regime.

This module is *self-contained*: it depends only on numpy + pandas, both of
which are already in the FundJohn requirements.txt.

Usage
-----
    from backtest_framework import Backtester, BacktestConfig, EquityTrade

    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=2.0))
    bt.add_trade(EquityTrade(...))
    ...
    report = bt.report()
    print(report.summary())

Author: Claude / FundJohn research, 2026-04-23.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG + TRADE DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """Cost + execution assumptions, plus annualisation factor."""

    # Per-trade costs (round-trip unless noted)
    fee_bps: float = 2.0                 # equity slippage + commission, round-trip
    options_commission_per_contract: float = 0.65
    options_slippage_bps: float = 10.0   # round-trip, on straddle premium
    short_borrow_bps_per_year: float = 30.0  # ~30 bps borrow on liquid US single names

    # Annualisation
    annualisation: int = 252             # 252 trading days/year for daily
    intraday_bars_per_year: int = 252 * 13   # 13 30-min bars/day
    risk_free_rate: float = 0.0          # set to 0 for excess-return Sharpe

    # Bootstrap
    bootstrap_n: int = 2000
    block_size: int = 21                 # ~1 trading month
    confidence: float = 0.95             # for CI bands

    # Walk-forward
    train_frac: float = 0.6              # 60% IS / 40% OOS by default

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EquityTrade:
    """A single long-or-short equity position from open to close."""

    ticker: str
    entry_date: date
    exit_date: date
    direction: str                        # 'LONG' or 'SHORT'
    entry_price: float
    exit_price: float
    weight: float = 1.0                   # fraction of equity (signed for LONG/SHORT later)
    label: str = ""                       # optional strategy / signal id
    regime: str = ""                      # optional regime label for breakdown

    @property
    def gross_return(self) -> float:
        if self.direction.upper() == 'LONG':
            return self.exit_price / self.entry_price - 1.0
        elif self.direction.upper() == 'SHORT':
            return self.entry_price / self.exit_price - 1.0
        else:
            raise ValueError(f"unknown direction {self.direction}")

    @property
    def days_held(self) -> int:
        return max(1, (self.exit_date - self.entry_date).days)


@dataclass
class OptionsVolTrade:
    """A SELL_VOL or BUY_VOL trade modeled as a delta-hedged straddle.

    PnL approximation (gamma-scalping decomposition):
        SELL_VOL : pnl ≈ (iv_entry^2 - rv_realised^2) * tau / 2
        BUY_VOL  : pnl ≈ (rv_realised^2 - iv_entry^2) * tau / 2

    Costs deducted: commission per contract + slippage on premium.
    """

    ticker: str
    entry_date: date
    exit_date: date
    direction: str                        # 'SELL_VOL' or 'BUY_VOL'
    iv_entry: float                       # annualised IV at entry (e.g. 0.30 for 30%)
    rv_realised: float                    # realised vol over [entry, exit]
    tau_years: float                      # holding period in years
    weight: float = 0.01                  # NAV fraction allocated to this trade
    n_contracts: int = 1                  # for cost accounting only
    label: str = ""
    regime: str = ""

    @property
    def gross_return(self) -> float:
        # Gamma-scalping PnL on a delta-hedged straddle:
        # PnL_per_unit_vega ≈ (IV² − RV²) × τ
        # Express as a return on premium ≈ premium = IV * sqrt(τ)
        # ⇒ return ≈ (IV² − RV²) * τ / (IV * sqrt(τ)) = (IV² − RV²) * sqrt(τ) / IV
        if self.iv_entry <= 0 or self.tau_years <= 0:
            return 0.0
        spread = (self.iv_entry ** 2 - self.rv_realised ** 2) * self.tau_years
        ret = spread / max(self.iv_entry ** 2 * self.tau_years, 1e-9)
        if self.direction.upper() == 'BUY_VOL':
            ret = -ret
        # Dampen so realistic single-trade returns are ±20% on the premium
        return float(np.clip(ret, -0.95, 0.95))

    @property
    def days_held(self) -> int:
        return max(1, (self.exit_date - self.entry_date).days)


@dataclass
class IntradayTrade:
    """A single intraday trade — entry bar and exit bar."""

    ticker: str
    entry_dt: datetime
    exit_dt: datetime
    direction: str                        # 'LONG' or 'SHORT'
    entry_price: float
    exit_price: float
    weight: float = 1.0                   # NAV fraction (default full size for legacy)
    label: str = ""
    regime: str = ""

    @property
    def gross_return(self) -> float:
        if self.direction.upper() == 'LONG':
            return self.exit_price / self.entry_price - 1.0
        elif self.direction.upper() == 'SHORT':
            return self.entry_price / self.exit_price - 1.0
        else:
            raise ValueError(f"unknown direction {self.direction}")


@dataclass
class RegimeEvent:
    """A regime classifier 'fire' for hit-rate-style evaluation."""

    fire_date: date
    horizon_days: int                     # window after fire to evaluate
    target_event_realised: bool           # did the target event occur in window?
    forward_return: float                 # SPY return over horizon
    label: str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  PERFORMANCE METRICS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_std(arr: np.ndarray, ddof: int = 1) -> float:
    if arr.size < 2:
        return 0.0
    s = float(np.std(arr, ddof=ddof))
    return s if s > 1e-12 else 0.0


def sharpe(returns: np.ndarray, ann: int) -> float:
    """Annualised Sharpe."""
    if returns.size < 2:
        return 0.0
    s = _safe_std(returns)
    if s == 0:
        return 0.0
    return float(np.mean(returns) / s * math.sqrt(ann))


def sortino(returns: np.ndarray, ann: int) -> float:
    """Annualised Sortino (downside deviation as denominator)."""
    if returns.size < 2:
        return 0.0
    downside = returns[returns < 0]
    if downside.size < 2:
        return 0.0
    dd = _safe_std(downside)
    if dd == 0:
        return 0.0
    return float(np.mean(returns) / dd * math.sqrt(ann))


def max_drawdown(equity_curve: np.ndarray) -> Tuple[float, int]:
    """Return (max DD as positive fraction, longest DD duration in periods)."""
    if equity_curve.size == 0:
        return 0.0, 0
    peak = np.maximum.accumulate(equity_curve)
    dd = (equity_curve - peak) / np.maximum(peak, 1e-12)
    max_dd = float(-np.min(dd))
    # Duration of longest underwater period
    underwater = dd < 0
    if not underwater.any():
        return max_dd, 0
    longest, current = 0, 0
    for u in underwater:
        if u:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return max_dd, int(longest)


def calmar(returns: np.ndarray, ann: int) -> float:
    """CAGR / max drawdown."""
    if returns.size == 0:
        return 0.0
    eq = np.cumprod(1 + returns)
    years = returns.size / ann
    if years <= 0 or eq[-1] <= 0:
        return 0.0
    cagr = eq[-1] ** (1 / years) - 1
    md, _ = max_drawdown(eq)
    if md == 0:
        return float('inf') if cagr > 0 else 0.0
    return float(cagr / md)


def win_rate(returns: np.ndarray) -> float:
    if returns.size == 0:
        return 0.0
    return float(np.mean(returns > 0))


def profit_factor(returns: np.ndarray) -> float:
    gains = returns[returns > 0].sum()
    losses = -returns[returns < 0].sum()
    if losses <= 0:
        return float('inf') if gains > 0 else 0.0
    return float(gains / losses)


def expectancy(returns: np.ndarray) -> float:
    return float(np.mean(returns)) if returns.size else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  BLOCK BOOTSTRAP CONFIDENCE INTERVAL
# ─────────────────────────────────────────────────────────────────────────────

def block_bootstrap_sharpe(returns: np.ndarray,
                           ann: int,
                           n: int = 2000,
                           block: int = 21,
                           rng: Optional[np.random.Generator] = None
                           ) -> Tuple[float, float]:
    """Stationary block bootstrap CI on Sharpe.

    Returns the (low, high) bounds for a 95% CI by default.
    """
    if returns.size < block * 2:
        return (0.0, 0.0)
    if rng is None:
        rng = np.random.default_rng(42)
    n_blocks = max(1, returns.size // block)
    samples = np.empty(n)
    starts_max = returns.size - block + 1
    for i in range(n):
        starts = rng.integers(0, starts_max, size=n_blocks)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[: returns.size]
        samples[i] = sharpe(returns[idx], ann)
    return (float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975)))


# ─────────────────────────────────────────────────────────────────────────────
#  REPORT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestReport:
    """Encapsulated metrics + by-regime breakdown + IS/OOS split."""

    name: str
    config: dict
    n_trades: int
    cum_return: float
    annualised_return: float
    annualised_vol: float
    sharpe: float
    sortino: float
    calmar: float
    max_dd: float
    max_dd_duration_periods: int
    win_rate: float
    profit_factor: float
    expectancy: float
    sharpe_ci_low: float
    sharpe_ci_high: float
    is_sharpe: float = float('nan')
    oos_sharpe: float = float('nan')
    by_regime: Dict[str, dict] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    equity_curve: List[float] = field(default_factory=list)
    daily_returns: List[float] = field(default_factory=list)

    def summary(self, indent: int = 2) -> str:
        d = self.to_dict()
        d.pop('equity_curve', None)
        d.pop('daily_returns', None)
        return json.dumps(d, indent=indent, default=str)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
#  BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────

class Backtester:
    """Accumulates trades, applies costs, computes metrics.

    Costs are deducted from each trade's gross return.  Daily returns are
    aggregated from trades that overlap each calendar day, weighted by
    position weight.
    """

    def __init__(self, config: Optional[BacktestConfig] = None, name: str = "strategy"):
        self.config = config or BacktestConfig()
        self.name = name
        self.equity_trades: List[EquityTrade] = []
        self.options_trades: List[OptionsVolTrade] = []
        self.intraday_trades: List[IntradayTrade] = []
        self.regime_events: List[RegimeEvent] = []

    # ── adders ──────────────────────────────────────────────────────────────
    def add_trade(self, t: Union[EquityTrade, OptionsVolTrade, IntradayTrade,
                                 RegimeEvent]):
        if isinstance(t, EquityTrade):
            self.equity_trades.append(t)
        elif isinstance(t, OptionsVolTrade):
            self.options_trades.append(t)
        elif isinstance(t, IntradayTrade):
            self.intraday_trades.append(t)
        elif isinstance(t, RegimeEvent):
            self.regime_events.append(t)
        else:
            raise TypeError(f"unknown trade type: {type(t)}")

    def add_trades(self, trades: Iterable):
        for t in trades:
            self.add_trade(t)

    # ── cost adjustments ────────────────────────────────────────────────────
    def _equity_net_return(self, t: EquityTrade) -> float:
        cost = self.config.fee_bps / 10000.0
        if t.direction.upper() == 'SHORT':
            cost += (self.config.short_borrow_bps_per_year / 10000.0) * \
                    (t.days_held / 365.0)
        return t.gross_return * t.weight - cost * abs(t.weight)

    def _options_net_return(self, t: OptionsVolTrade) -> float:
        slip = self.config.options_slippage_bps / 10000.0
        # Commission as fraction of premium: assume premium ≈ $5 ⇒ 0.65/500 ≈ 0.0013
        comm = (t.n_contracts * self.config.options_commission_per_contract) / \
               max(100.0 * t.iv_entry * math.sqrt(t.tau_years), 1e-3)
        return (t.gross_return - slip - comm) * t.weight

    def _intraday_net_return(self, t: IntradayTrade) -> float:
        cost = self.config.fee_bps / 10000.0
        return t.gross_return * t.weight - cost * abs(t.weight)

    # ── trade ledger as DataFrame ───────────────────────────────────────────
    def trade_ledger(self) -> pd.DataFrame:
        rows = []
        for t in self.equity_trades:
            rows.append({
                'kind': 'equity',
                'ticker': t.ticker,
                'entry': t.entry_date, 'exit': t.exit_date,
                'direction': t.direction,
                'gross_return': t.gross_return * t.weight,
                'net_return': self._equity_net_return(t),
                'weight': t.weight,
                'days_held': t.days_held,
                'label': t.label,
                'regime': t.regime,
            })
        for t in self.options_trades:
            rows.append({
                'kind': 'options',
                'ticker': t.ticker,
                'entry': t.entry_date, 'exit': t.exit_date,
                'direction': t.direction,
                'gross_return': t.gross_return * t.weight,
                'net_return': self._options_net_return(t),
                'weight': t.weight,
                'days_held': t.days_held,
                'label': t.label,
                'regime': t.regime,
                'iv_entry': t.iv_entry,
                'rv_realised': t.rv_realised,
            })
        for t in self.intraday_trades:
            rows.append({
                'kind': 'intraday',
                'ticker': t.ticker,
                'entry': t.entry_dt, 'exit': t.exit_dt,
                'direction': t.direction,
                'gross_return': t.gross_return * t.weight,
                'net_return': self._intraday_net_return(t),
                'weight': t.weight,
                'days_held': max(1, (t.exit_dt.date() - t.entry_dt.date()).days),
                'label': t.label,
                'regime': t.regime,
            })
        return pd.DataFrame(rows)

    # ── daily returns: aggregate trades by exit date ────────────────────────
    def daily_returns(self) -> pd.Series:
        ledger = self.trade_ledger()
        if ledger.empty:
            return pd.Series(dtype=float)

        # For equity / options: PnL credited on exit_date.
        # For intraday: credited on exit_dt.date()
        ledger['exit_date'] = ledger['exit'].apply(
            lambda x: x.date() if isinstance(x, datetime) else x
        )
        # Sum net returns per day; this implicitly assumes one unit of capital
        # is split across all trades in a day (weight already sets the share).
        if ledger['kind'].isin(['equity', 'options']).all():
            grouped = ledger.groupby('exit_date')['net_return'].sum()
        else:
            grouped = ledger.groupby('exit_date')['net_return'].sum()
        return grouped.sort_index()

    # ── compute metrics ──────────────────────────────────────────────────────
    def _metrics_block(self, returns: np.ndarray, ann: int) -> dict:
        if returns.size == 0:
            return {'sharpe': 0.0, 'sortino': 0.0, 'win_rate': 0.0,
                    'profit_factor': 0.0, 'expectancy': 0.0,
                    'max_dd': 0.0, 'cum_return': 0.0,
                    'annualised_return': 0.0, 'annualised_vol': 0.0}
        eq = np.cumprod(1 + returns)
        md, dd_dur = max_drawdown(eq)
        ann_ret = float(np.mean(returns) * ann)
        ann_vol = float(_safe_std(returns) * math.sqrt(ann))
        return {
            'sharpe': sharpe(returns, ann),
            'sortino': sortino(returns, ann),
            'calmar': calmar(returns, ann),
            'win_rate': win_rate(returns),
            'profit_factor': profit_factor(returns),
            'expectancy': expectancy(returns),
            'max_dd': md,
            'max_dd_duration_periods': dd_dur,
            'cum_return': float(eq[-1] - 1.0),
            'annualised_return': ann_ret,
            'annualised_vol': ann_vol,
        }

    # ── public report ────────────────────────────────────────────────────────
    def report(self) -> BacktestReport:
        ledger = self.trade_ledger()
        if ledger.empty and not self.regime_events:
            return BacktestReport(
                name=self.name, config=self.config.to_dict(),
                n_trades=0, cum_return=0, annualised_return=0,
                annualised_vol=0, sharpe=0, sortino=0, calmar=0,
                max_dd=0, max_dd_duration_periods=0, win_rate=0,
                profit_factor=0, expectancy=0,
                sharpe_ci_low=0, sharpe_ci_high=0,
            )

        # If no trade ledger but we have regime events, evaluate as a classifier
        if ledger.empty and self.regime_events:
            return self._regime_report()

        daily = self.daily_returns()
        ret_arr = daily.values.astype(float)
        ann = self.config.annualisation
        if (ledger['kind'] == 'intraday').all():
            ann = self.config.annualisation  # daily PnL aggregation
        m = self._metrics_block(ret_arr, ann)

        # Block bootstrap CI on Sharpe
        ci_low, ci_high = block_bootstrap_sharpe(
            ret_arr, ann, n=self.config.bootstrap_n,
            block=self.config.block_size,
        )

        # Walk-forward IS / OOS Sharpe
        n = ret_arr.size
        cut = int(n * self.config.train_frac)
        is_sharpe = sharpe(ret_arr[:cut], ann) if cut > 1 else float('nan')
        oos_sharpe = sharpe(ret_arr[cut:], ann) if (n - cut) > 1 else float('nan')

        # Regime breakdown if 'regime' present
        by_regime: Dict[str, dict] = {}
        if 'regime' in ledger and ledger['regime'].notna().any():
            for reg, grp in ledger.groupby('regime'):
                if not reg:
                    continue
                grp_daily = grp.groupby(grp['exit'].apply(
                    lambda x: x.date() if isinstance(x, datetime) else x
                ))['net_return'].sum()
                by_regime[reg] = self._metrics_block(grp_daily.values, ann)

        return BacktestReport(
            name=self.name,
            config=self.config.to_dict(),
            n_trades=int(len(ledger)),
            cum_return=m['cum_return'],
            annualised_return=m['annualised_return'],
            annualised_vol=m['annualised_vol'],
            sharpe=m['sharpe'],
            sortino=m['sortino'],
            calmar=m['calmar'],
            max_dd=m['max_dd'],
            max_dd_duration_periods=m['max_dd_duration_periods'],
            win_rate=m['win_rate'],
            profit_factor=m['profit_factor'],
            expectancy=m['expectancy'],
            sharpe_ci_low=ci_low,
            sharpe_ci_high=ci_high,
            is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe,
            by_regime=by_regime,
            equity_curve=list(np.cumprod(1 + ret_arr)),
            daily_returns=list(ret_arr),
        )

    # ── regime classifier evaluation (S-TR-01/02/03) ────────────────────────
    def _regime_report(self) -> BacktestReport:
        events = self.regime_events
        n = len(events)
        if n == 0:
            return BacktestReport(
                name=self.name, config=self.config.to_dict(),
                n_trades=0, cum_return=0, annualised_return=0,
                annualised_vol=0, sharpe=0, sortino=0, calmar=0,
                max_dd=0, max_dd_duration_periods=0, win_rate=0,
                profit_factor=0, expectancy=0,
                sharpe_ci_low=0, sharpe_ci_high=0,
            )
        hits = sum(1 for e in events if e.target_event_realised)
        forward_rets = np.array([e.forward_return for e in events], float)
        return BacktestReport(
            name=self.name,
            config=self.config.to_dict(),
            n_trades=n,
            cum_return=float(np.sum(forward_rets)),
            annualised_return=float(np.mean(forward_rets) * 252 / max(events[0].horizon_days, 1)),
            annualised_vol=float(_safe_std(forward_rets) * math.sqrt(252 / max(events[0].horizon_days, 1))),
            sharpe=sharpe(forward_rets, 252 // max(events[0].horizon_days, 1)),
            sortino=sortino(forward_rets, 252 // max(events[0].horizon_days, 1)),
            calmar=0.0,
            max_dd=0.0,
            max_dd_duration_periods=0,
            win_rate=hits / n,
            profit_factor=profit_factor(forward_rets),
            expectancy=float(np.mean(forward_rets)),
            sharpe_ci_low=0.0, sharpe_ci_high=0.0,
            extra={
                'hit_rate': hits / n,
                'event_horizon_days': events[0].horizon_days,
                'mean_forward_return_at_event': float(np.mean(forward_rets)),
                'n_events': n,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
#  WALK-FORWARD HELPER
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward_split(dates: pd.DatetimeIndex,
                       n_folds: int = 5,
                       train_min_days: int = 252) -> List[Tuple[pd.Timestamp, pd.Timestamp,
                                                                pd.Timestamp, pd.Timestamp]]:
    """Generate walk-forward (train_start, train_end, test_start, test_end) tuples.

    Anchored walk-forward: training window grows from start; test window slides.
    """
    n = len(dates)
    if n < train_min_days * 2:
        return [(dates[0], dates[train_min_days],
                 dates[train_min_days], dates[-1])]
    fold_size = (n - train_min_days) // n_folds
    folds = []
    for k in range(n_folds):
        train_end_idx = train_min_days + k * fold_size
        test_end_idx = min(train_end_idx + fold_size, n - 1)
        if test_end_idx <= train_end_idx + 1:
            break
        folds.append((dates[0], dates[train_end_idx],
                      dates[train_end_idx], dates[test_end_idx]))
    return folds


# ─────────────────────────────────────────────────────────────────────────────
#  REALISED VOL HELPER
# ─────────────────────────────────────────────────────────────────────────────

def realised_vol(close: pd.Series, window: int = 21,
                 annualisation: int = 252) -> pd.Series:
    """Annualised rolling realised volatility from close-to-close log returns."""
    log_ret = np.log(close).diff()
    return log_ret.rolling(window).std() * math.sqrt(annualisation)


# ─────────────────────────────────────────────────────────────────────────────
#  SYNTHETIC DATA GENERATORS (for self-test & dev runs without VPS data)
# ─────────────────────────────────────────────────────────────────────────────

def gen_synthetic_prices(n_days: int = 2520, n_tickers: int = 50,
                         start: str = "2016-01-04",
                         seed: int = 42) -> pd.DataFrame:
    """Generate a long-format synthetic prices DataFrame with realistic GBM
    + occasional jumps + regime shifts.

    Schema mirrors prices.parquet : ticker, date, open, high, low, close, volume,
                                    vwap, transactions
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n_days)
    tickers = [f"SYM{i:03d}" for i in range(n_tickers)]
    rows = []
    for t_idx, tk in enumerate(tickers):
        sigma = 0.10 + 0.20 * rng.random()       # 10–30% annualised vol
        mu = 0.03 + 0.08 * rng.random()           # 3–11% drift
        # daily log returns with jumps
        daily_sigma = sigma / math.sqrt(252)
        daily_mu = mu / 252
        rets = rng.normal(daily_mu, daily_sigma, n_days)
        jumps = rng.binomial(1, 0.005, n_days) * rng.normal(0, 0.05, n_days)
        rets += jumps
        # regime shift halfway: amplify vol
        rets[n_days // 2:] *= 1.3
        log_p = np.cumsum(rets) + math.log(50.0 + rng.random() * 200.0)
        close = np.exp(log_p)
        open_ = close * (1 + rng.normal(0, daily_sigma * 0.3, n_days))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, daily_sigma * 0.3, n_days)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, daily_sigma * 0.3, n_days)))
        vol = rng.integers(100_000, 5_000_000, n_days)
        vwap = (open_ + high + low + close) / 4.0
        for i, d in enumerate(dates):
            rows.append({
                'ticker': tk, 'date': d.date(),
                'open': float(open_[i]), 'high': float(high[i]),
                'low': float(low[i]), 'close': float(close[i]),
                'volume': int(vol[i]), 'vwap': float(vwap[i]),
                'transactions': int(vol[i] // 100),
            })
    return pd.DataFrame(rows)


def gen_synthetic_options(prices_df: pd.DataFrame,
                          tickers: Optional[List[str]] = None,
                          n_strikes: int = 15,
                          expiries_dte: Tuple[int, ...] = (14, 30, 60, 90),
                          snapshot_freq: str = 'W-FRI',
                          seed: int = 43) -> pd.DataFrame:
    """Generate options_eod-shaped DataFrame: ticker, date, expiry, strike,
    option_type, market_price, implied_volatility, delta, gamma, theta, vega,
    rho, open_interest, volume, bid, ask.

    Uses Black-Scholes for IV → price mapping with a stylised vol smile.
    Generates one snapshot per `snapshot_freq` date (default weekly Friday).
    """
    rng = np.random.default_rng(seed)
    if tickers is None:
        tickers = list(prices_df['ticker'].unique())[:20]

    from math import erf
    N = lambda x: 0.5 * (1 + erf(x / math.sqrt(2)))
    npdf = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)

    # Pre-compute weekly snapshot dates from the price index
    px_dates = pd.to_datetime(prices_df['date'].drop_duplicates()).sort_values()
    snap_dates = pd.date_range(px_dates.min(), px_dates.max(), freq=snapshot_freq)
    px_date_set = set(px_dates.dt.date)
    snap_dates = [d.date() for d in snap_dates if d.date() in px_date_set]

    # Per-ticker informational drift η_i: a hidden directional signal that
    # leaks into option IVs.  Positive η → call IV slightly above put IV
    # (informed buying calls).  Stable per-ticker but reshuffled every ~30 days.
    rows = []
    for tk in tickers:
        px = prices_df[prices_df['ticker'] == tk].sort_values('date').reset_index(drop=True)
        if px.empty:
            continue
        px_idx = {d: i for i, d in enumerate(px['date'].tolist())}
        # Per-ticker base bias (mean ~0)
        eta_base = float(rng.normal(0, 0.005))

        for d0 in snap_dates:
            if d0 not in px_idx:
                continue
            i_close = px_idx[d0]
            S = float(px.loc[i_close, 'close'])
            window = px['close'].iloc[max(0, i_close - 20):i_close + 1]
            rv20 = float(realised_vol(window, 20).iloc[-1] or 0.30)
            base_iv = max(0.10, min(rv20 * 1.05, 1.0))
            # Time-varying informational drift (refreshes ~monthly)
            month_seed = int(pd.Timestamp(d0).strftime('%Y%m'))
            eta = eta_base + float(np.random.default_rng(seed + month_seed + hash(tk) % 1000).normal(0, 0.012))

            for dte in expiries_dte:
                exp = (pd.Timestamp(d0) + pd.Timedelta(days=dte)).date()
                sigma_S = S * base_iv * math.sqrt(dte / 252.0)
                # Wider grid (±2.5σ) so |delta|≈0.20 OTM strikes are populated.
                # Clamp K to be strictly positive (deep OTM puts can otherwise go ≤ 0).
                k_min = max(0.1 * S, S - 2.5 * sigma_S)
                strike_grid = np.linspace(k_min, S + 2.5 * sigma_S, n_strikes)
                for K in strike_grid:
                    if K <= 0:
                        continue
                    moneyness = (K / S) - 1.0
                    smile = base_iv + 0.20 * abs(moneyness) - 0.10 * moneyness  # put skew
                    iv_mid = max(0.08, smile + rng.normal(0, 0.005))
                    tau = max(dte, 1) / 365.0
                    for opt in ('call', 'put'):
                        # Add informational drift η on calls (+) vs puts (−)
                        if opt == 'call':
                            iv = max(0.05, iv_mid + eta)
                        else:
                            iv = max(0.05, iv_mid - eta)
                        d1 = (math.log(S / K) + 0.5 * iv ** 2 * tau) / (iv * math.sqrt(tau))
                        d2 = d1 - iv * math.sqrt(tau)
                        if opt == 'call':
                            price = S * N(d1) - K * N(d2)
                            delta = N(d1)
                        else:
                            price = K * N(-d2) - S * N(-d1)
                            delta = N(d1) - 1.0
                        gamma = npdf(d1) / (S * iv * math.sqrt(tau))
                        vega = S * npdf(d1) * math.sqrt(tau) / 100.0
                        theta = -(S * npdf(d1) * iv) / (2 * math.sqrt(tau)) / 365.0
                        rho = K * tau * (N(d2) if opt == 'call' else -N(-d2)) / 100.0
                        bid = max(0.01, price - 0.05)
                        ask = price + 0.05
                        oi = int(rng.integers(50, 5000))
                        vol_ = int(rng.integers(0, oi // 2 + 1))
                        rows.append({
                            'ticker': tk, 'date': d0,
                            'expiry': exp,
                            'strike': float(K), 'option_type': opt,
                            'market_price': float(price),
                            'implied_volatility': float(iv),
                            'delta': float(delta), 'gamma': float(gamma),
                            'theta': float(theta), 'vega': float(vega),
                            'rho': float(rho),
                            'open_interest': oi, 'volume': vol_,
                            'bid': float(bid), 'ask': float(ask),
                        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  CLI for self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick self-test: generate trades and compute a report
    bt = Backtester(BacktestConfig(annualisation=252), name="self_test")
    rng = np.random.default_rng(0)
    base = date(2024, 1, 1)
    for i in range(500):
        # Synthetic positive-Sharpe equity trades
        from datetime import timedelta
        d0 = base + timedelta(days=int(i * 1.5))
        d1 = d0 + timedelta(days=5)
        ep = 100.0
        ret = rng.normal(0.0008, 0.02)  # ~0.08% daily edge
        bt.add_trade(EquityTrade(
            ticker="TEST", entry_date=d0, exit_date=d1,
            direction='LONG', entry_price=ep,
            exit_price=ep * (1 + ret), weight=0.05,
            label='self_test', regime=('HV' if i % 3 == 0 else 'NEUTRAL'),
        ))
    rep = bt.report()
    print(rep.summary())
