"""OPENCLAW_ACCOUNT_BREAKER_DD_RULE=0 disables ONLY the lifetime drawdown rule
(operator ruling 2026-11-27); the daily-loss rule and the ARM semantics are
untouched."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab  # noqa: E402


def test_default_env_still_fires_the_drawdown_rule(monkeypatch):
    monkeypatch.delenv(ab.DD_RULE_ENV, raising=False)
    st = ab.evaluate(-0.1386, 83_635.0, 83_535.0)
    assert st['rule'] == 'drawdown' and st['breach'] is True


def test_flag_zero_suppresses_drawdown_but_keeps_the_measure(monkeypatch):
    monkeypatch.setenv(ab.DD_RULE_ENV, '0')
    st = ab.evaluate(-0.1386, 83_635.0, 83_535.0)
    assert st['rule'] == 'none' and st['breach'] is False
    assert abs(st['dd'] + 0.1386) < 1e-9          # still reported on the line


def test_flag_zero_daily_loss_rule_still_fires(monkeypatch):
    monkeypatch.setenv(ab.DD_RULE_ENV, '0')
    st = ab.evaluate(-0.20, 96_000.0, 100_000.0)   # -4 % on the day
    assert st['rule'] == 'daily_loss' and st['breach'] is True


def test_flag_one_or_garbage_means_enabled(monkeypatch):
    for v in ('1', '', 'yes'):
        monkeypatch.setenv(ab.DD_RULE_ENV, v)
        assert ab.dd_rule_enabled() is True
    monkeypatch.setenv(ab.DD_RULE_ENV, ' 0 ')
    assert ab.dd_rule_enabled() is False
