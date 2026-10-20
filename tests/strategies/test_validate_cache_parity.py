"""D2b -- OPENCLAW_BT_CACHE_PARITY cached-vs-fresh assert. Synthetic only."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import validate_strategy as vs  # noqa: E402

_CAUSAL = "lambda p: p.rolling(10).mean()"
_NONCAUSAL = "lambda p: p - p.mean()"

_BODY = """
        out = []
        if len(prices) < {lb}:
            return out
        ind = self.cached('ind', {fn}, prices)
        last = ind.iloc[-1]
        for t in list(prices.columns)[:3]:
            px = float(prices[t].iloc[-1])
            v = float(last[t]) if last[t] == last[t] else 0.0
            out.append(Signal(ticker=t, direction='LONG', entry_price=px,
                              stop_loss=px * 0.98, target_1=px * 1.05,
                              target_2=px * 1.10, target_3=px * 1.15,
                              confidence='MED', position_size_pct=0.01,
                              signal_params={{'v': round(v, 6)}}))
        return out
"""

_NEVER = """
        return []
        self.cached('ind', lambda p: p, prices)
"""


def _write(tmp_path, name, body, lb=20):
    src = textwrap.dedent(f'''
        from typing import List
        from strategies.base import BaseStrategy, Signal


        class {name}(BaseStrategy):
            id = '{name.lower()}'
            name = '{name}'
            description = 'cache parity fixture'
            tier = 3
            min_lookback = {lb}
            active_in_regimes = ['LOW_VOL']

            def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:''')
    src += body
    p = tmp_path / f'{name.lower()}.py'
    p.write_text(src)
    return str(p)


def _body(fn, lb=20):
    return _BODY.format(fn=fn, lb=lb)


def test_flag_unset_no_parity_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('OPENCLAW_BT_CACHE_PARITY', raising=False)
    res = vs.validate(_write(tmp_path, 'ZzOff', _body(_NONCAUSAL)))
    assert res['ok'] is True
    assert set(res) == {'ok', 'errors', 'signal_count', 'warnings'}
    assert res['warnings'] == []
    assert '[cache_parity]' not in capsys.readouterr().err


def test_causal_cached_strategy_passes(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_CACHE_PARITY', '1')
    res = vs.validate(_write(tmp_path, 'ZzCausal', _body(_CAUSAL)))
    assert res['ok'] is True, res['errors']
    assert 'cache_parity_vacuous' not in res['warnings']


def test_noncausal_cached_strategy_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_CACHE_PARITY', '1')
    res = vs.validate(_write(tmp_path, 'ZzNonCausal', _body(_NONCAUSAL)))
    assert res['ok'] is False
    assert any(e.startswith('cache_parity:') for e in res['errors'])


def test_long_lookback_panel_is_long_enough(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OPENCLAW_BT_CACHE_PARITY', '1')
    res = vs.validate(_write(tmp_path, 'ZzLong', _body(_CAUSAL, 252), lb=252))
    assert res['ok'] is True, res['errors']
    assert 'cache_parity_vacuous' not in res['warnings']
    assert 'checked=1 dates=3' in capsys.readouterr().err


def test_vacuous_warning_when_cached_never_reached(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_CACHE_PARITY', '1')
    res = vs.validate(_write(tmp_path, 'ZzVacuous', _NEVER))
    assert res['ok'] is True, res['errors']
    assert 'cache_parity_vacuous' in res['warnings']


def test_stderr_line_emitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OPENCLAW_BT_CACHE_PARITY', '1')
    vs.validate(_write(tmp_path, 'ZzErr', _body(_CAUSAL)))
    assert '[cache_parity] checked=1 dates=3 errors=0' in capsys.readouterr().err
