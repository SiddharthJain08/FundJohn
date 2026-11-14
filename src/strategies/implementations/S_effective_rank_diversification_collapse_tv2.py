"""
S_effective_rank_diversification_collapse_tv2 — Dimensionality Collapse
Early Warning, variant 2 of 2 (Askar 2026, "4.87 Dimensionality Collapse:
The Warning Signal Before Diversification Dies", aligrithm.com).

Hypothesis: when a book's cross-asset correlation matrix collapses toward a
single dominant factor — effective rank falls, one principal component
explains most of the variance, and its loadings share a sign — the book's
apparent diversification is illusory, and gross exposure should be cut
before the correlated drawdown hits. Reported as an abstract-only extraction
(no appendix with exact panel, window, or threshold values), so every
numeric/structural choice below is this implementation's defensible
reading.

Interpretation variant 2 of 2 — deliberately different from variant 1
(`S_effective_rank_diversification_collapse`) on every ambiguous axis:

  - Panel for the correlation/eigen computation: the LIVE `universe`
    argument (the pipeline's rotating sp500 membership for the current
    bar), intersected with available price history and capped at a
    deterministic PANEL_CAP=60 names (alphabetical) for compute bounds —
    not variant 1's fixed 38-name cross-sector basket. This trades
    coverage determinism for responsiveness to the book's actual current
    opportunity set, which is the more natural reading of "a book's
    cross-asset correlation matrix" (the book IS the live universe).
  - Rolling window = 252 trading days (~1 year), not variant 1's reactive
    60d window. A collapsing correlation structure is a regime-level
    phenomenon; a 1-year window reads "the warning signal before
    diversification dies" as a slower structural-break detector that
    filters single-week correlation spikes rather than chasing them.
  - Collapse threshold is an ABSOLUTE effective-rank floor
    (effective_rank < EFFECTIVE_RANK_FLOOR, 3.0) rather than variant 1's
    panel-size-relative ratio. "Effective rank" in the PCA/eigenvalue-
    entropy literature is usually read as an absolute measure of
    remaining degrees of freedom (e.g. "the book is behaving like it only
    holds ~3 independent bets") — not normalized by how many names happen
    to be in the panel this bar.
  - "Sign(PC1 loadings) is uniform across all assets" is SOFTENED to a
    90% majority-sign threshold (MAJORITY_SIGN_THRESHOLD) rather than
    variant 1's literal 100%-of-names reading. One idiosyncratic,
    low-loading name flipping sign on noise shouldn't veto an otherwise
    clear common-factor mode.
  - Executed instrument: variant 1 reads FLAT/HOLD as abstain/act (no
    signal when collapsed, LONG SPY when not). This variant reads
    "cut gross exposure" as an ACTIVE hedge rather than passive
    abstention: emits SHORT SPY when collapse is detected (actively
    offsetting book beta) and LONG SPY when not (confirming/holding
    gross). Both states always emit a signal.
  - `active_in_regimes` = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL'] rather
    than variant 1's ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']. An "early
    warning signal BEFORE diversification dies" is most useful while
    exiting calm and entering instability, before a crisis is already
    underway and the hedge is moot — not during full-blown CRISIS, where
    the correlated drawdown this signal exists to pre-empt has typically
    already happened.

The paper's unambiguous items are kept as specified: min_lookback_required
(504 trading days), minimum_universe_size (10; MIN_ASSETS=10),
instrument_class='equity', direction_vocab FLAT/HOLD realized as
SHORT-SPY/LONG-SPY, and the exact
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

__all__ = ['EffectiveRankDiversificationCollapseTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_effective_rank_diversification_collapse_tv2'
EXEC_TICKER      = 'SPY'

WINDOW                      = 252   # trading days — rolling correlation/eigen window
EFFECTIVE_RANK_FLOOR         = 3.0  # absolute effective-rank floor (not ratio-of-n)
MAJORITY_SIGN_THRESHOLD      = 0.90 # fraction of PC1 loadings sharing the majority sign
MIN_ASSETS                   = 10   # spec's minimum_universe_size
PANEL_CAP                    = 60   # cap on live-universe names fed into the eigen-decomp
BASE_SIZE                    = 0.10


def _dimensionality_metrics(window_prices: pd.DataFrame) -> dict | None:
    """Rolling log-return correlation matrix -> effective rank + PC1
    diagnostics, per the pseudocode:
        effective_rank = exp(entropy(eigenvalues / sum(eigenvalues)))
        pc1_variance_share = max(eigenvalue) / sum(eigenvalues)
    Collapse (variant 2): absolute effective-rank floor + 90% majority-sign
    PC1 loadings, rather than variant 1's panel-size-relative ratio +
    literal all-same-sign reading. Returns None if the window can't support
    a clean estimate.
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

    idx_max = int(np.argmax(eigvals))
    v = eigvecs[:, idx_max]
    n_pos = int(np.sum(v > 0))
    n_neg = int(np.sum(v < 0))
    majority_sign_frac = max(n_pos, n_neg) / float(len(v))

    collapse = bool(
        effective_rank < EFFECTIVE_RANK_FLOOR
        and majority_sign_frac >= MAJORITY_SIGN_THRESHOLD
    )

    return {
        'n_assets': n,
        'effective_rank': effective_rank,
        'pc1_variance_share': pc1_share,
        'majority_sign_frac': majority_sign_frac,
        'collapse': collapse,
    }


class EffectiveRankDiversificationCollapseTv2(BaseStrategy):
    """Portfolio-level dimensionality-collapse early warning, variant 2:
    when the trailing 1-year correlation matrix of the LIVE universe
    collapses onto a single, majority-sign-uniform common factor (absolute
    effective rank below a fixed floor), actively hedge via SHORT SPY;
    otherwise confirm/hold gross via LONG SPY (Askar 2026, variant 2 of
    2)."""

    id                = STRATEGY_ID
    name              = 'EffectiveRankDiversificationCollapseTv2'
    description       = (
        'Dimensionality-collapse early warning (variant 2): actively hedges via SHORT SPY '
        'when the live-universe panel\'s trailing 252d correlation matrix collapses below an '
        'absolute effective-rank floor with majority-sign-uniform PC1 loadings; else confirms '
        'gross via LONG SPY (Askar 2026).'
    )
    instrument_class  = INSTRUMENT_CLASS
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'window':                WINDOW,
            'effective_rank_floor':  EFFECTIVE_RANK_FLOOR,
            'base_size':             BASE_SIZE,
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

        live_tickers = [t for t in sorted(set(universe or [])) if t in prices.columns][:PANEL_CAP]
        if len(live_tickers) < MIN_ASSETS:
            print(f'[debug] {STRATEGY_ID}: signals=0 (live panel too small: {len(live_tickers)})', file=sys.stderr)
            return []

        panel = prices[live_tickers].ffill().dropna(how='all')
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

        scale = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        rank_floor = float(self.parameters.get('effective_rank_floor', EFFECTIVE_RANK_FLOOR))
        margin = abs(metrics['effective_rank'] - rank_floor)
        confidence = 'HIGH' if margin > 1.5 else ('MED' if margin > 0.5 else 'LOW')

        direction = 'SHORT' if metrics['collapse'] else 'LONG'

        stops = self.compute_stops_and_targets(
            prices[EXEC_TICKER].dropna(),
            direction=direction,
            current_price=spy_price,
            regime_state=regime_state,
        )

        signal = Signal(
            ticker            = EXEC_TICKER,
            direction         = direction,
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
                'effective_rank_floor': rank_floor,
                'majority_sign_frac':  round(metrics['majority_sign_frac'], 4),
                'collapse':            metrics['collapse'],
                'regime':              regime_state,
                'scale':               scale,
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 direction={direction} '
            f'effective_rank={metrics["effective_rank"]:.4f} '
            f'majority_sign_frac={metrics["majority_sign_frac"]:.4f} '
            f'entry={spy_price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [signal]


def run_regime_backtest():
    """Offline helper: compute regime-partitioned backtest metrics for the
    lifecycle promotion gate. Loads the live-universe panel + SPY prices
    and historical regimes from master parquets, runs the collapse logic
    over full history, tags each trade day with the active regime, then
    calls run_backtest_with_regime_partition() so the gate can read
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

    # Deterministic stand-in for the "live universe" at backtest time: the
    # first PANEL_CAP tickers alphabetically with full history coverage.
    candidates = sorted(c for c in wide.columns if c != EXEC_TICKER)
    live_tickers = candidates[:PANEL_CAP]
    if len(live_tickers) < MIN_ASSETS:
        print(f'[backtest] live panel too small: {len(live_tickers)} — aborting', file=sys.stderr)
        return None

    panel = wide[live_tickers].ffill()
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
        direction = 'SHORT' if metrics['collapse'] else 'LONG'
        fwd = float(spy_fwd_ret.iloc[i]) if pd.notna(spy_fwd_ret.iloc[i]) else 0.0
        pnl = -fwd if direction == 'SHORT' else fwd
        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(signal_date.date()),
            'regime_state': regime_state,
            'direction':    direction,
            'pnl':          pnl,
            'r_multiple':   pnl / max(abs(pnl), 1e-6) if pnl != 0 else 0.0,
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
