"""oxf_bollinger_momentum_dsl - the expression-DSL twin of
``oxf_bollinger_momentum`` (oxfordstrat Bollinger-band momentum breakout on the
ETF basket): LONG above SMA80 + 2 sigma, SHORT below SMA80 - 2 sigma.

Original: ``oxf_bollinger_momentum.OxfBollingerMomentum``.

Threshold derivation: the original's sigma is the POPULATION std (ddof=0);
the DSL ``std`` is ddof=1.  s1 = s0 * sqrt(n / (n - 1)), so
``close - mid > 2 * s0``  <=>  ``(close - mid) / s1 > 2 * sqrt((n-1) / n)``
with n = 80, i.e. B = 2 * sqrt(79 / 80).

Divergences from the original (documented, not bugs):
  - A ticker absent on the evaluation row emits nothing (the original uses its
    stale last value).
  - The panel is the UNION basket calendar; the original evaluates each ETF on
    its own bars.
  - Confidence is a flat 'MED' and sizing is SIGNAL_BASE_SIZE-based.
"""
from __future__ import annotations

import math

from strategies.oxford_crabel import OxfordBaseStrategy

__all__ = ['OxfBollingerMomentumDsl']

_B = 2.0 * math.sqrt(79 / 80)


class OxfBollingerMomentumDsl(OxfordBaseStrategy):
    id                = 'oxf_bollinger_momentum_dsl'
    name              = 'Oxford Bollinger Band Momentum (DSL)'
    description       = ('Bollinger-band momentum breakout on liquid ETFs as a SIGNAL formula '
                         '(oxfordstrat bollinger-bands). DSL twin of oxf_bollinger_momentum.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 100
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    SIGNAL           = "(close - sma(close, 80)) / std(close, 80)"
    SIGNAL_DIRECTION = 'both'
    SIGNAL_THRESHOLD = (-_B, _B)
    SIGNAL_BASE_SIZE = 0.05

    def default_parameters(self) -> dict:
        return {'ma_len': 80, 'k': 2.0}

    def signal_panel(self, prices, universe, aux_data):
        import pandas as pd
        from strategies.signal_expr import make_panel
        ohlc = self.basket_ohlc(prices)      # ignore `universe` - self-loaded basket
        if not ohlc:
            return None
        close = pd.concat({t: b['close'] for t, b in ohlc.items()}, axis=1).sort_index()
        return make_panel({'close': close})
