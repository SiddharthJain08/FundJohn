"""
MLP-Initialized Reinforcement Learning for Exit Timing in SPX Short-Straddle
Portfolios — Wang, Ślepaczuk, Sakowski (2026).
Source: http://arxiv.org/abs/2610.13126v1

Hypothesis: exit timing, not entry, is the dominant lever in short-straddle
P&L. An oracle-guided (ex-post optimal hold/exit label) supervised MLP,
RL-fine-tuned, learns a state-dependent exit policy — conditioned on
time-to-expiry, running P&L, and the realized/implied vol gap — that beats
both passive holding and fixed-time / fixed-PnL-threshold exit rules.

Implementation notes:
  - SPX/^GSPC carries no option chain in the master parquet; SPY is the
    tradable, chain-covered proxy of the same underlying beta (precedent:
    S_vix_gamma_range_width, S_model_free_iv_forecast,
    S_svi_vol_surface_relative_value).
  - The RL policy itself cannot be trained inside a deterministic, no-I/O
    strategy module (no torch/sklearn-training allowlisted, no persisted
    weights), and `BaseStrategy.should_exit` (the per-bar exit hook) is
    explicitly out of scope for options legs (per-bar-exit-hook spec,
    2026-08-28, §"Out of scope") — `src/backtest/options_backtest.py`'s
    cycle pricer never calls it and always reports
    `signal_stop`/`signal_target` as None, so a price-level or should_exit
    based "exit policy" would be silent dead code for an option-instrument
    strategy. The only exit-timing lever the options engine actually honors
    is `OptionSpec.roll_dte` (roll once DTE remaining <= roll_dte) against
    `OptionSpec.dte_target`.
  - This module reproduces the paper's *shape* of the learned policy within
    that constraint: `roll_dte` is set PER SIGNAL at entry time, conditioned
    on the same state family the paper's exit policy conditions on at
    decision time — here, the entry-time vol risk premium (iv30 - rv_20)
    and iv_rank, which proxy how much theta edge is actually priced in.
    A deep edge (rich vrp, high iv_rank) is let run closer to expiry
    (small roll_dte) to harvest more of the convex decay curve; a thin edge
    is rolled earlier (large roll_dte) to cut gamma exposure before it can
    erase a small priced cushion. This is a one-shot, entry-time proxy for
    the paper's per-bar policy, not a reinforcement-learned one.

Signal rule (daily, entry):
  vrp = iv30 - rv_20   (vol risk premium on SPY options)
  vrp > VRP_ENTRY_MIN  -> SELL_VOL (short ATM straddle), roll_dte set by
                          conviction (see ROLL_DTE_BY_CONFIDENCE)
  else                 -> no signal (FLAT)

Reported metrics: Sharpe 1.705, out-of-sample, 2024-2026 backtest window
(overfitting flag: short_backtest — paper's own OOS window is short).
"""
from __future__ import annotations
import math
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['SpxStraddleRlExitTiming']

INSTRUMENT_CLASS = 'option'
STRATEGY_ID      = 'S_spx_straddle_rl_exit_timing'

UNDERLYING        = 'SPY'      # SPX proxy — no SPX option chain in master parquet
VRP_ENTRY_MIN     = 0.03       # min (iv30 - rv_20) to sell vol
DTE_TARGET        = 30
BASE_SIZE         = 0.02
MAX_SIZE          = 0.05

# Entry-time proxy for the paper's state-dependent exit policy: the only
# exit-timing lever the options engine honors is roll_dte vs dte_target
# (src/backtest/options_backtest.py — price-level stop/target are ignored
# for priced option cycles). Deeper entry-time conviction -> smaller
# roll_dte (hold closer to expiry, more theta captured); thin conviction ->
# larger roll_dte (roll out earlier, less gamma exposure on a small edge).
ROLL_DTE_BY_CONFIDENCE = {'HIGH': 5, 'MED': 9, 'LOW': 14}


class SpxStraddleRlExitTiming(BaseStrategy):
    id                = STRATEGY_ID
    name              = 'SPX Short-Straddle RL Exit Timing'
    description       = ('Sells SPX (SPY-proxy) ATM straddles when the vol risk premium is positive; '
                          'approximates the paper\'s oracle-guided MLP+RL exit timing by conditioning the '
                          'engine\'s only honored exit-timing lever (roll_dte) on entry-time vrp/iv_rank '
                          'conviction instead of a fixed roll schedule (Wang, Slepaczuk, Sakowski 2026).')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        signals: List[Signal] = []
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return signals

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return signals

        series = prices[UNDERLYING].dropna()
        if len(series) < self.min_lookback:
            print(f'[debug] signals=0 (insufficient history: {len(series)})', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        opts = ((aux_data or {}).get('options') or {}).get(UNDERLYING) or {}
        iv30 = opts.get('iv30')
        rv20 = opts.get('rv_20')
        vrp  = opts.get('vrp')
        if vrp is None and iv30 is not None and rv20 is not None:
            vrp = float(iv30) - float(rv20)
        if vrp is None or iv30 is None:
            print('[debug] signals=0 (no iv30/vrp)', file=sys.stderr)
            return signals

        vrp = float(vrp)
        if vrp <= VRP_ENTRY_MIN:
            print(f'[debug] signals=0 (vrp={vrp:.4f} <= {VRP_ENTRY_MIN})', file=sys.stderr)
            return signals

        iv_rank = float(opts.get('iv_rank') or 50.0)
        scale = self.position_scale(regime_state)
        confidence = 'HIGH' if (vrp >= 0.06 and iv_rank >= 60.0) else ('MED' if vrp >= VRP_ENTRY_MIN * 1.5 else 'LOW')
        size = min(BASE_SIZE * scale * (1.0 + min(vrp / 0.06, 1.5)), MAX_SIZE)
        roll_dte = ROLL_DTE_BY_CONFIDENCE[confidence]

        stops = self.compute_stops_and_targets(
            series, 'SHORT', current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying      = UNDERLYING,
            right           = 'call',
            strike_rule     = 'atm',
            dte_target      = DTE_TARGET,
            structure       = 'straddle',
            hedge           = 'none',
            roll_dte        = roll_dte,
            hold_to_expiry  = False,
        )

        signals.append(Signal(
            ticker            = UNDERLYING,
            direction         = 'SELL_VOL',
            entry_price       = round(current_price, 4),
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'iv30_entry':    round(float(iv30), 4),
                'rv20_entry':    round(float(rv20), 4) if rv20 is not None else None,
                'vrp_entry':     round(vrp, 4),
                'iv_rank_entry': round(iv_rank, 2),
                'dte_target':    DTE_TARGET,
                'roll_dte':      roll_dte,
                'hold_days':     DTE_TARGET - roll_dte,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]


# ---------------------------------------------------------------------------
# Regime-partitioned backtest (lightweight proxy, precedent:
# S_vix_gamma_range_width.py's __main__ — realized-move-vs-priced-expected-
# move proxy, not the full priced-cycle options engine).
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_df, _bars = load_prices_panels(tickers=[UNDERLYING])
    reg_series = load_regimes()
    strat = SpxStraddleRlExitTiming()
    spy = prices_df[UNDERLYING].dropna()
    idx = spy.index
    rows = []
    i = max(strat.min_lookback, 252)
    while i < len(idx) - 1:
        d = idx[i]
        prices_upto = prices_df.iloc[: i + 1]
        aux = load_aux_data(d.strftime('%Y-%m-%d'))
        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        sigs = strat.generate_signals(prices_upto, {'state': rstate}, [UNDERLYING], aux_data=aux)
        if not sigs:
            i += 1
            continue
        sig = sigs[0]
        entry_price = float(spy.loc[d])

        # Hold until the signal's own roll point (dte_target - roll_dte),
        # capped to the data available.
        hold_days = max(int(sig.signal_params['hold_days']), 1)
        j = min(i + hold_days, len(idx) - 1)
        if j <= i:
            i += 1
            continue

        exit_price = float(spy.loc[idx[j]])
        iv30_entry = sig.signal_params.get('iv30_entry') or 0.0
        expected_move = iv30_entry * math.sqrt(hold_days / 252.0)
        realized_move = abs(exit_price - entry_price) / entry_price
        pnl = expected_move - realized_move  # SELL_VOL profits when realized < priced
        rows.append({
            'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
            'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
        })
        i = j + 1

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
