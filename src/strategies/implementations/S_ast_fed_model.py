"""
FED Model — Yield-Gap OLS predictor for SPY vs SHY rotation.

Source: https://quantpedia.com/strategies/fed-model/

Monthly rebalance. Computes yield gap YG = ln(SP500_earnings_yield) - ln(US10Y_bond_yield).
Maintains rolling history of monthly (market_return, rf_rate, YG) observations and runs OLS:
  excess_return[t] ~ alpha + beta * YG[t-1]
If forecasted next-month excess return > 0: hold SPY. Else: hold SHY.

SP500 earnings yield is approximated from SPY price with a seeded earnings growth model
(initial EY 5% at 2016-04-11, growing 3%/yr nominal).

Inputs (2026-08-23 re-point): the 10Y yield and the risk-free rate come from
macro.parquet — aux_data['macro']['DGS10'] and ['DGS3MO'] (FRED keyless stream,
percent units, month-end resampled, point-in-time in backtests) — with the old
inputs kept as fallbacks: ^TNX from prices (frozen since 2026-05-21) then a
fixed 3.5% for the bond yield; the `rf_rate_annual` parameter (2%) for rf.
signal_params records which source was used (bond_yield_source / rf_source).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstFedModel']

INSTRUMENT_CLASS = 'etp'

# Fixed 2-asset universe
BASKET = ('SPY', 'SHY')

# Parameters
INITIAL_EY = 0.05          # seed SP500 earnings yield at 2016 (PE~20)
EARNINGS_GROWTH = 0.03     # nominal annual earnings growth rate
RF_RATE_ANNUAL = 0.02      # FALLBACK risk-free when macro DGS3MO is absent
BOND_YIELD_FALLBACK = 0.035  # FALLBACK 10Y yield when macro DGS10 and ^TNX are both absent
BOND_SERIES = 'DGS10'      # macro.parquet series (FRED, percent)
RF_SERIES = 'DGS3MO'       # macro.parquet series (FRED, percent)


def _monthly_from_macro(macro, name: str, index: pd.DatetimeIndex):
    """aux_data['macro'][name] (daily, percent) → month-end-last series aligned
    to `index` with forward-fill. None when the series is missing or empty so
    callers can fall back. Point-in-time safety comes from the loader: in
    backtests the series is already truncated to <= the as-of date."""
    if not macro or name not in macro:
        return None
    ser = macro[name]
    if ser is None or len(ser) == 0:
        return None
    ser = pd.Series(ser).dropna()
    if ser.empty:
        return None
    if not isinstance(ser.index, pd.DatetimeIndex):
        ser.index = pd.to_datetime(ser.index)
    monthly = ser.sort_index().resample('ME').last()
    return monthly.reindex(index, method='ffill')
MIN_OBS = 6                # minimum monthly observations for OLS
WARMUP_MONTHS = 12         # months before first signal


class AstFedModel(BaseStrategy):
    """FED Model: hold SPY when OLS-forecasted excess return vs yield-gap > 0, else SHY.

    Source: https://quantpedia.com/strategies/fed-model/
    """

    id                = 'S_ast_fed_model'
    name              = 'FED Model (Yield Gap OLS)'
    description       = (
        'Monthly rebalance: OLS of SP500 excess returns on yield gap (earnings yield '
        'minus 10Y bond yield); long SPY when forecast > 0, else long SHY.'
    )
    instrument_class  = 'etp'
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 252
    # Works across regimes — it is a macro timing model that switches between risk
    # and safety; apply regime scaling rather than filtering entirely.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'initial_ey':       INITIAL_EY,
            'earnings_growth':  EARNINGS_GROWTH,
            'rf_rate_annual':   RF_RATE_ANNUAL,
            'min_obs':          MIN_OBS,
            'warmup_months':    WARMUP_MONTHS,
            'pos_size_frac':    0.20,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[AstFedModel] no price data — returning []', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[AstFedModel] regime {regime_state} not active — returning []', file=sys.stderr)
            return []

        params = self.parameters
        initial_ey      = float(params.get('initial_ey', INITIAL_EY))
        earnings_growth = float(params.get('earnings_growth', EARNINGS_GROWTH))
        rf_monthly      = float(params.get('rf_rate_annual', RF_RATE_ANNUAL)) / 12.0
        min_obs         = int(params.get('min_obs', MIN_OBS))
        warmup_months   = int(params.get('warmup_months', WARMUP_MONTHS))

        # ── 1. Extract SPY and ^TNX (10Y yield) price series ──────────────────
        spy_col = 'SPY' if 'SPY' in prices.columns else None
        tnx_col = '^TNX' if '^TNX' in prices.columns else None
        shy_col = 'SHY' if 'SHY' in prices.columns else None

        if spy_col is None:
            print('[AstFedModel] SPY not in prices — returning []', file=sys.stderr)
            return []

        spy = prices[spy_col].dropna()
        # Ensure DatetimeIndex for resampling
        if not isinstance(spy.index, pd.DatetimeIndex):
            spy.index = pd.to_datetime(spy.index)
        if len(spy) < 252:
            print(f'[AstFedModel] SPY only {len(spy)} bars < 252 — returning []', file=sys.stderr)
            return []

        # ── 2. Build monthly series ────────────────────────────────────────────
        # Resample to month-end
        spy_monthly = spy.resample('ME').last().dropna()
        if len(spy_monthly) < warmup_months + min_obs:
            print(f'[AstFedModel] only {len(spy_monthly)} monthly bars — returning []', file=sys.stderr)
            return []

        # Approximate SP500 earnings yield: seed EY at first date, grow earnings 3%/yr
        first_date  = spy_monthly.index[0]
        first_price = float(spy_monthly.iloc[0])
        seed_earnings = first_price * initial_ey  # E(first_date)

        t_years = (spy_monthly.index - first_date).days / 365.25
        earnings_est = seed_earnings * np.exp(earnings_growth * t_years)
        sp500_ey = earnings_est / spy_monthly.values  # EY(t) = E(t) / P(t)

        # 10Y bond yield (decimal): macro DGS10 → ^TNX close → fixed fallback.
        macro = (aux_data or {}).get('macro') or {}
        bond_macro = _monthly_from_macro(macro, BOND_SERIES, spy_monthly.index)
        if bond_macro is not None and bond_macro.notna().any():
            bond_yield_monthly = bond_macro / 100.0
            bond_source = f'macro:{BOND_SERIES}'
        elif tnx_col is not None:
            tnx = prices[tnx_col].dropna()
            if not isinstance(tnx.index, pd.DatetimeIndex):
                tnx.index = pd.to_datetime(tnx.index)
            tnx_monthly = tnx.resample('ME').last()
            bond_yield_monthly = tnx_monthly.reindex(spy_monthly.index, method='ffill') / 100.0
            bond_source = 'prices:^TNX'
        else:
            bond_yield_monthly = pd.Series(BOND_YIELD_FALLBACK, index=spy_monthly.index)
            bond_source = f'fixed:{BOND_YIELD_FALLBACK}'

        # Risk-free (monthly, decimal): macro DGS3MO time series → fixed parameter.
        rf_macro = _monthly_from_macro(macro, RF_SERIES, spy_monthly.index)
        if rf_macro is not None and rf_macro.notna().any():
            rf_monthly_series = (rf_macro / 100.0 / 12.0).fillna(rf_monthly)
            rf_source = f'macro:{RF_SERIES}'
        else:
            rf_monthly_series = pd.Series(rf_monthly, index=spy_monthly.index)
            rf_source = f'fixed:{float(params.get("rf_rate_annual", RF_RATE_ANNUAL))}'

        # Yield gap: YG = ln(EY) - ln(bond_yield)
        # Guard against non-positive values
        valid_mask = (sp500_ey > 0) & (bond_yield_monthly.values > 0)
        yg_values = np.where(
            valid_mask,
            np.log(sp500_ey) - np.log(bond_yield_monthly.values),
            np.nan,
        )
        yg_series = pd.Series(yg_values, index=spy_monthly.index)

        # Monthly market returns
        spy_rets = spy_monthly.pct_change()

        # Excess returns (rf aligned month by month)
        excess_rets = spy_rets - rf_monthly_series

        # Combine into a DataFrame and drop NaNs
        df = pd.DataFrame({
            'excess_ret': excess_rets,
            'yg':         yg_series,
        }).dropna()

        if len(df) < min_obs + 1:
            print(f'[AstFedModel] insufficient observations ({len(df)}) — returning []', file=sys.stderr)
            return []

        # ── 3. Rolling OLS using all available history up to current month ─────
        # Use all data except the last row as training; last row's yg as current signal
        # The spec: OLS of excess_return[t] ~ alpha + beta * YG[t-1]
        # So X = YG shifted by 1, Y = excess_return
        df_lagged = pd.DataFrame({
            'y': df['excess_ret'].values[1:],
            'x': df['yg'].values[:-1],
        })

        if len(df_lagged) < min_obs:
            print(f'[AstFedModel] insufficient lagged obs ({len(df_lagged)}) — returning []', file=sys.stderr)
            return []

        # OLS: y = alpha + beta * x
        x = df_lagged['x'].values
        y = df_lagged['y'].values
        # Use numpy polyfit (degree 1) for OLS
        try:
            coeffs = np.polyfit(x, y, 1)  # returns [beta, alpha]
            beta, alpha = float(coeffs[0]), float(coeffs[1])
        except (np.linalg.LinAlgError, ValueError) as e:
            print(f'[AstFedModel] OLS failed: {e} — returning []', file=sys.stderr)
            return []

        # Current yield gap = last available yg observation
        yg_current = float(df['yg'].iloc[-1])

        # Forecast next-month excess return
        y_hat = alpha + beta * yg_current

        # ── 4. Emit signal ─────────────────────────────────────────────────────
        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('pos_size_frac', 0.20)) * scale, 4)

        current_spy = float(spy.iloc[-1])

        if y_hat > 0:
            # Bullish: hold SPY
            ticker    = 'SPY'
            direction = 'LONG'
            price     = current_spy
            confidence = 'HIGH' if y_hat > 0.005 else 'MED'
        else:
            # Bearish: hold SHY (risk-free proxy)
            if shy_col is not None:
                shy = prices[shy_col].dropna()
                price = float(shy.iloc[-1]) if len(shy) > 0 else 84.0
            else:
                price = 84.0  # approximate SHY price
            ticker    = 'SHY'
            direction = 'LONG'
            confidence = 'MED'

        # Use compute_stops_and_targets for ATR-based bracket
        price_series = spy if ticker == 'SPY' else (prices[shy_col].dropna() if shy_col else spy)
        if len(price_series) < 14:
            print('[AstFedModel] price series too short for ATR — returning []', file=sys.stderr)
            return []

        stops = self.compute_stops_and_targets(
            price_series,
            direction='LONG',
            current_price=price,
            regime_state=regime_state,
        )

        signal = Signal(
            ticker            = ticker,
            direction         = direction,
            entry_price       = float(price),
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'bond_yield_source':  bond_source,
                'rf_source':          rf_source,
                'bond_yield_current': round(float(bond_yield_monthly.dropna().iloc[-1]), 6) if bond_yield_monthly.notna().any() else None,
                'rf_annual_current':  round(float(rf_monthly_series.dropna().iloc[-1]) * 12.0, 6) if rf_monthly_series.notna().any() else None,
                'y_hat':        round(y_hat, 6),
                'yg_current':   round(yg_current, 4),
                'alpha':        round(alpha, 6),
                'beta':         round(beta, 6),
                'n_obs':        len(df_lagged),
                'regime':       regime_state,
                'scale':        scale,
            },
        )

        print(
            f'[AstFedModel] ticker={ticker} y_hat={y_hat:.4f} yg={yg_current:.4f} '
            f'alpha={alpha:.4f} beta={beta:.4f} n_obs={len(df_lagged)} '
            f'entry={price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        signals = [signal]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
