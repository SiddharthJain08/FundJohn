"""
S_effective_rank_diversification_collapse — Dimensionality Collapse Early
Warning (Askar 2026, "4.87 Dimensionality Collapse: The Warning Signal
Before Diversification Dies", aligrithm.com).

Hypothesis: when a book's cross-asset correlation matrix collapses toward a
single dominant factor — effective rank falls, one principal component
explains most of the variance, and its loadings share a sign — the book's
apparent diversification is illusory, and gross exposure should be cut
before the correlated drawdown hits. Reported as an abstract-only extraction
(no appendix with exact panel, window, or threshold values), so every
numeric/structural choice below is this implementation's defensible
reading.

Interpretation variant 1 of 2 — deliberately different from a colleague's
plausible variant 2 read of the same ambiguous spec:

  - Panel for the correlation/eigen computation: a FIXED, deterministic
    ~38-name cross-GICS-sector large-cap equity basket (tech, financials,
    healthcare, energy, industrials, staples, discretionary, utilities,
    materials, real estate, telecom) rather than the live `universe`
    argument's rotating sp500 membership. A colleague could as defensibly
    walk the live universe each bar; this variant trades that responsiveness
    for a liquid, always-available, deterministic coverage floor so the
    rank/PC1 estimate never swings on universe churn alone.
  - Rolling window = 60 trading days (~3 months), not the 252d/504d a
    colleague might pick for a more stable estimate — "the warning signal
    BEFORE diversification dies" reads as an early-warning indicator, which
    argues for a shorter, more reactive estimation window over a slower one.
  - Collapse threshold is defined RELATIVE to panel size —
    effective_rank / n_assets < RANK_RATIO_THRESHOLD (0.30) — rather than an
    absolute effective-rank floor (e.g. "< 3"). A colleague could as
    defensibly fix an absolute cutoff; this variant's ratio form is
    panel-size invariant, which matters if panel coverage drifts with data
    availability.
  - "Sign(PC1 loadings) is uniform across all assets" is read LITERALLY: all
    n loadings must share sign (within float tolerance). A colleague could
    as defensibly soften this to "≥90% share the majority sign" to tolerate
    one idiosyncratic name; this variant keeps the paper's "across all
    assets" wording strict.
  - Executed instrument: the paper's FLAT/HOLD vocabulary is a portfolio-
    level gross-exposure overlay, not a per-name directional call, so it is
    expressed as a single SPY LONG (HOLD — keep the book's equity gross) vs.
    no signal (FLAT — cut gross exposure) decision, mirroring the existing
    S_stock_bond_corr_crash_warning precedent for portfolio-level de-risking
    overlays in this codebase. MAX_SIGNALS=1 follows from that read.

The paper's unambiguous items are kept as specified: min_lookback_required
(504 trading days), minimum_universe_size (10; this implementation's panel
floor is MIN_ASSETS=10), instrument_class='equity', direction_vocab
FLAT/HOLD realized as no-signal/LONG-SPY, and the exact
effective_rank = exp(entropy(eigenvalues / sum(eigenvalues))) /
pc1_variance_share = max(eigenvalue) / sum(eigenvalues) formulas from the
pseudocode. Data: close-price panel only (`prices`) — log_returns are
computed in-process, not a stored column (both already_in_ledger per spec).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import EPS

__all__ = ['EffectiveRankDiversificationCollapse']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_effective_rank_diversification_collapse'
EXEC_TICKER      = 'SPY'

# Fixed cross-GICS-sector large-cap panel used to estimate the book's
# correlation structure (variant 1 — not the live rotating sp500 universe).
PANEL = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META',   # tech / comm
    'JPM', 'BAC', 'GS', 'MS',                          # financials
    'JNJ', 'PFE', 'UNH', 'ABBV',                       # healthcare
    'XOM', 'CVX', 'COP',                               # energy
    'HON', 'CAT', 'UPS', 'BA',                         # industrials
    'PG', 'KO', 'WMT', 'PEP',                          # staples
    'HD', 'MCD', 'NKE', 'SBUX',                        # discretionary
    'NEE', 'DUK',                                      # utilities
    'LIN', 'FCX',                                      # materials
    'PLD', 'AMT',                                      # real estate
    'VZ', 'T',                                         # telecom
]

WINDOW              = 60     # trading days — rolling correlation/eigen window
RANK_RATIO_THRESHOLD = 0.30  # effective_rank / n_assets below this ⇒ collapse
SIGN_TOL             = 1e-9  # numerical tolerance for "all loadings same sign"
MIN_ASSETS           = 10    # spec's minimum_universe_size
BASE_SIZE            = 0.10


def _dimensionality_metrics(window_prices: pd.DataFrame) -> dict | None:
    """Rolling log-return correlation matrix -> effective rank + PC1
    diagnostics, per the pseudocode:
        effective_rank = exp(entropy(eigenvalues / sum(eigenvalues)))
        pc1_variance_share = max(eigenvalue) / sum(eigenvalues)
    Returns None if the window can't support a clean estimate.
    """
    log_ret = np.log(window_prices / window_prices.shift(1)).dropna(how='any')
    log_ret = log_ret.dropna(axis=1, how='any')
    n = log_ret.shape[1]
    if len(log_ret) < WINDOW // 2 or n < MIN_ASSETS:
        return None

    corr = log_ret.corr().to_numpy()
    corr = np.nan_to_num(corr, nan=0.0)
    corr = (corr + corr.T) / 2.0
    try:
        eigvals, eigvecs = np.linalg.eigh(corr)
    except np.linalg.LinAlgError:
        return None
    eigvals = np.clip(eigvals, 0.0, None)   # numerical noise guard
    total = float(eigvals.sum())
    if total <= EPS:
        return None

    p = eigvals / total
    mask = p > EPS
    entropy = -float(np.sum(p[mask] * np.log(p[mask])))
    effective_rank = float(np.exp(entropy))
    pc1_share = float(eigvals.max() / total)
    rank_ratio = effective_rank / n

    idx_max = int(np.argmax(eigvals))
    v = eigvecs[:, idx_max]
    sign_uniform = bool(np.all(v >= -SIGN_TOL) or np.all(v <= SIGN_TOL))

    collapse = bool(rank_ratio < RANK_RATIO_THRESHOLD and sign_uniform)

    return {
        'n_assets': n,
        'effective_rank': effective_rank,
        'pc1_variance_share': pc1_share,
        'rank_ratio': rank_ratio,
        'sign_uniform': sign_uniform,
        'collapse': collapse,
    }


class EffectiveRankDiversificationCollapse(BaseStrategy):
    """Portfolio-level dimensionality-collapse early warning: when the
    trailing correlation matrix of a fixed cross-sector equity panel
    collapses onto a single, sign-uniform common factor (low effective rank
    relative to panel size), cut equity gross exposure (no signal / FLAT);
    otherwise hold the book's current gross exposure (LONG SPY) (Askar 2026,
    variant 1 of 2)."""

    id                = STRATEGY_ID
    name              = 'EffectiveRankDiversificationCollapse'
    description       = (
        'Dimensionality-collapse early warning: cuts equity gross exposure when a fixed '
        'cross-sector panel\'s trailing 60d correlation matrix collapses onto a low-rank, '
        'sign-uniform common factor; else holds gross via a LONG SPY signal (Askar 2026).'
    )
    instrument_class  = INSTRUMENT_CLASS
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'window':              WINDOW,
            'rank_ratio_threshold': RANK_RATIO_THRESHOLD,
            'base_size':           BASE_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[debug] {STRATEGY_ID}: signals=0 (empty prices)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] {STRATEGY_ID}: signals=0 (regime={regime_state} excluded)', file=sys.stderr)
            return []

        if EXEC_TICKER not in prices.columns:
            print(f'[debug] {STRATEGY_ID}: signals=0 (SPY missing)', file=sys.stderr)
            return []

        tickers = [t for t in PANEL if t in prices.columns]
        if len(tickers) < MIN_ASSETS:
            print(f'[debug] {STRATEGY_ID}: signals=0 (panel too small: {len(tickers)})', file=sys.stderr)
            return []

        panel = prices[tickers].ffill().dropna(how='all')
        if len(panel) < self.min_lookback:
            print(f'[debug] {STRATEGY_ID}: signals=0 (history rows={len(panel)})', file=sys.stderr)
            return []

        window = int(self.parameters.get('window', WINDOW))
        window_panel = panel.tail(window + 1)
        metrics = _dimensionality_metrics(window_panel)
        if metrics is None:
            print(f'[debug] {STRATEGY_ID}: signals=0 (metrics unavailable)', file=sys.stderr)
            return []

        spy_price = float(prices[EXEC_TICKER].dropna().iloc[-1])
        if spy_price <= 0:
            print(f'[debug] {STRATEGY_ID}: signals=0 (invalid SPY price)', file=sys.stderr)
            return []

        if metrics['collapse']:
            print(
                f'[debug] {STRATEGY_ID}: signals=0 (collapse rank_ratio={metrics["rank_ratio"]:.4f} '
                f'sign_uniform={metrics["sign_uniform"]} — FLAT)',
                file=sys.stderr,
            )
            return []

        scale = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        rank_ratio_threshold = float(self.parameters.get('rank_ratio_threshold', RANK_RATIO_THRESHOLD))
        margin = metrics['rank_ratio'] - rank_ratio_threshold
        confidence = 'HIGH' if margin > 0.30 else ('MED' if margin > 0.10 else 'LOW')

        stops = self.compute_stops_and_targets(
            prices[EXEC_TICKER].dropna(),
            direction='LONG',
            current_price=spy_price,
            regime_state=regime_state,
        )

        signal = Signal(
            ticker            = EXEC_TICKER,
            direction         = 'LONG',
            entry_price       = spy_price,
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'n_assets':            metrics['n_assets'],
                'effective_rank':      round(metrics['effective_rank'], 4),
                'pc1_variance_share':  round(metrics['pc1_variance_share'], 4),
                'rank_ratio':          round(metrics['rank_ratio'], 4),
                'rank_ratio_threshold': rank_ratio_threshold,
                'sign_uniform':        metrics['sign_uniform'],
                'regime':              regime_state,
                'scale':               scale,
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 rank_ratio={metrics["rank_ratio"]:.4f} '
            f'pc1_share={metrics["pc1_variance_share"]:.4f} entry={spy_price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [signal]


def run_regime_backtest():
    """Offline helper: compute regime-partitioned backtest metrics for the
    lifecycle promotion gate. Loads the panel + SPY prices and historical
    regimes from master parquets, runs the collapse-threshold logic over
    full history, tags each trade day with the active regime, then calls
    run_backtest_with_regime_partition() so the gate can read
    result['eligible_regimes_proposed']."""
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    base = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    prices_path = os.path.join(base, 'prices.parquet')
    regime_path = os.path.join(base, 'historical_regimes.parquet')

    raw = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    wide = raw.pivot(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)

    if EXEC_TICKER not in wide.columns:
        print('[backtest] SPY not in prices — aborting', file=sys.stderr)
        return None

    tickers = [t for t in PANEL if t in wide.columns]
    if len(tickers) < MIN_ASSETS:
        print(f'[backtest] panel too small: {len(tickers)} — aborting', file=sys.stderr)
        return None

    panel = wide[tickers].ffill()
    spy = wide[EXEC_TICKER]
    spy_fwd_ret = spy.pct_change().shift(-1)

    reg = pd.read_parquet(regime_path)
    reg['date'] = pd.to_datetime(reg['date'] if 'date' in reg.columns else reg.index)
    if 'date' in reg.columns:
        reg = reg.set_index('date')
    regime_col = next((c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None)

    rows = []
    for i in range(WINDOW + 1, len(panel) - 1):
        window_panel = panel.iloc[i - WINDOW - 1:i]
        metrics = _dimensionality_metrics(window_panel)
        if metrics is None:
            continue
        signal_date = panel.index[i]
        spy_price = spy.iloc[i]
        if pd.isna(spy_price) or spy_price <= 0:
            continue
        regime_state = 'LOW_VOL'
        if regime_col and signal_date in reg.index:
            regime_state = str(reg.loc[signal_date, regime_col])
        direction = 'FLAT' if metrics['collapse'] else 'LONG'
        pnl = float(spy_fwd_ret.iloc[i]) if direction == 'LONG' and pd.notna(spy_fwd_ret.iloc[i]) else 0.0
        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(signal_date.date()),
            'regime_state': regime_state,
            'direction':    direction,
            'pnl':          pnl,
            'r_multiple':   pnl / max(abs(pnl), 1e-6) if direction == 'LONG' else 0.0,
        })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}',
          file=sys.stderr)
    return result


if __name__ == '__main__':
    run_regime_backtest()
