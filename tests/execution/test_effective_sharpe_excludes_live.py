"""tests/test_effective_sharpe_excludes_live.py

live_sharpe must be RECORDED but must NOT influence effective_sharpe.

Why (operator directive 2026-07-16): effective_sharpe drives `weight`,
`daily_weight`, the S_adj conviction gate, and the activation filter. It used to
be a sample-size blend:

    effective_sharpe = (bt_n × bt_sharpe + live_n × live_sharpe) / (bt_n + live_n)

That is invalid for two independent reasons:

 1. ATTRIBUTION. The book takes AGGREGATED positions: many strategies signal the
    same ticker and the broker holds ONE position exiting through ONE
    Sharpe-weighted stacked bracket. If S1 wants stop −5%/target +3% and S2 wants
    −8%/+6%, the live exit happens at the blended level and BOTH strategies are
    booked that shared outcome. Neither strategy's own rule ever ran, so its
    per-strategy live pnl_pct measures the blend, not the strategy. A per-strategy
    live Sharpe is only measurable with a separate paper account where every
    strategy fires its own brackets independently — we do not have one.

 2. UNITS. bt_sharpe is an ANNUALIZED daily-return Sharpe
    ((mean − rf)/std × sqrt(252)). live_sharpe is a RAW per-TRADE ratio
    (mu/sigma over pnl_pct) with no time basis. Averaging them by trade count
    treats two different quantities as one.

live_sharpe / live_n stay computed and persisted: as active portfolio days
accumulate they can support a PORTFOLIO-level Sharpe, which is measurable
precisely because it does not require per-strategy attribution.

Measured before the change: of 70 current weight rows, 69 were bt-only, 1 was
blended (drag 0.00), and 0 were live-only — so this is inert today and bites
only as live trades accumulate.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.strategy_weights import _effective_sharpe  # noqa: E402


class TestEffectiveSharpeExcludesLive(unittest.TestCase):

    def test_live_does_not_move_effective_sharpe(self):
        """The blend is gone: effective == bt regardless of live."""
        bt = {'bt_sharpe': 1.80, 'bt_n': 100}
        live = {'live_sharpe': 0.05, 'live_n': 900}   # would have dominated the old blend
        eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(bt, live)
        self.assertAlmostEqual(eff, 1.80, places=9,
                               msg='effective_sharpe must equal bt_sharpe; live must not drag it')
        # Old behaviour would have been (100*1.8 + 900*0.05)/1000 = 0.225
        self.assertNotAlmostEqual(eff, 0.225, places=3)

    def test_live_is_still_recorded(self):
        """Excluded from the decision, retained for observability/portfolio use."""
        bt = {'bt_sharpe': 1.80, 'bt_n': 100}
        live = {'live_sharpe': 0.05, 'live_n': 900}
        eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(bt, live)
        self.assertEqual(lv_s, 0.05, 'live_sharpe must still be returned for persistence')
        self.assertEqual(lv_n, 900, 'live_n must still be returned for persistence')
        self.assertEqual(bt_s, 1.80)
        self.assertEqual(bt_n, 100)

    def test_bt_only_unchanged(self):
        eff, *_ = _effective_sharpe({'bt_sharpe': 0.9, 'bt_n': 50}, None)
        self.assertAlmostEqual(eff, 0.9)

    def test_live_only_is_NOT_sizeable(self):
        """No backtest → no measurable Sharpe → eff None → excluded from sizing.

        Deliberate behaviour change. Previously a live-only strategy rode
        live_sharpe into the book. Since per-strategy live Sharpe is not a real
        measurement (see module docstring), riding it would size on a number
        that measures the stacked-bracket blend. Measured 2026-07-16: 0 of 70
        current rows are live-only, so nothing deactivates today.
        """
        eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(None, {'live_sharpe': 2.5, 'live_n': 40})
        self.assertIsNone(eff, 'live-only must NOT produce a sizeable effective_sharpe')
        self.assertEqual(lv_s, 2.5, 'but live_sharpe is still recorded')
        self.assertEqual(lv_n, 40)

    def test_neither_is_none(self):
        eff, *_ = _effective_sharpe(None, None)
        self.assertIsNone(eff)

    def test_negative_bt_preserved(self):
        """Sign must survive: negative effective_sharpe excludes the regime."""
        eff, *_ = _effective_sharpe({'bt_sharpe': -0.4, 'bt_n': 30},
                                    {'live_sharpe': 3.0, 'live_n': 500})
        self.assertAlmostEqual(eff, -0.4,
                               msg='a good live run must not rescue a negative backtest')


if __name__ == '__main__':
    unittest.main()
