"""Exit-hook Phase 1 simulator tests (spec §2). Tasks 2–5 append here."""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import backtest.unified_backtest as ub


class TestBarExit:
    def test_long_stop_only(self):
        assert ub._bar_exit(1, high=101.0, low=94.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (95.0, 'stop')

    def test_long_target_only(self):
        assert ub._bar_exit(1, high=109.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (108.0, 'target')

    def test_long_neither(self):
        assert ub._bar_exit(1, high=101.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (None, None)

    def test_short_mirrors(self):
        assert ub._bar_exit(-1, high=106.0, low=99.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (105.0, 'stop')
        assert ub._bar_exit(-1, high=101.0, low=91.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (92.0, 'target')

    def test_double_touch_priority(self):
        both = dict(high=110.0, low=90.0, stop_loss=95.0, target_1=108.0)
        assert ub._bar_exit(1, dt_priority='stop', **both) == (95.0, 'stop')
        assert ub._bar_exit(1, dt_priority='target', **both) == (108.0, 'target')
