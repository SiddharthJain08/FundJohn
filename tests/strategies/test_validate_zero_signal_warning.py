"""D3 — validate_strategy's zero_signals_synthetic WARNING (spec 2026-09-12 §4 D3).

Synthetic only: each test writes a throwaway strategy module to a tmp path and
calls validate() on it. validate() falls back to spec_from_file_location for a
file outside SRC_DIR, so nothing is imported into the strategies package and no
DB / parquet is touched.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_validate_zero_signal_warning.py -q
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import validate_strategy as vs  # noqa: E402


_SILENT_BODY = """
        return []
"""

_EMITTING_BODY = """
        out = []
        for t in (universe or [])[:3]:
            if t not in prices.columns:
                continue
            px = float(prices[t].iloc[-1])
            out.append(Signal(ticker=t, direction='LONG', entry_price=px,
                              stop_loss=px * 0.98, target_1=px * 1.05,
                              target_2=px * 1.10, target_3=px * 1.15,
                              confidence='MED', position_size_pct=0.01))
        return out
"""


def _write_strategy(tmp_path: Path, name: str, *, attrs: str = '', body: str = _SILENT_BODY) -> str:
    src = textwrap.dedent(f'''
        from typing import List
        from strategies.base import BaseStrategy, Signal


        class {name}(BaseStrategy):
            id   = '{name.lower()}'
            name = '{name}'
            description = 'zero-signal warning fixture'
            tier = 3
        ''')
    for line in (attrs.strip().splitlines() if attrs.strip() else []):
        src += f'    {line.strip()}\n'
    src += (
        "\n    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:"
        f"{body}"
    )
    path = tmp_path / f'{name.lower()}.py'
    path.write_text(src)
    return str(path)


def test_zero_signals_sets_warning_and_never_blocks(tmp_path):
    path = _write_strategy(tmp_path, 'ZzSilent')
    res = vs.validate(path)
    assert res['ok'] is True, res['errors']
    assert res['signal_count'] == 0
    assert res['warnings'] == ['zero_signals_synthetic']


def test_calendar_edge_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzCalendar', attrs='calendar_edge = True')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_regime_gated_away_from_low_vol_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzCrisisOnly',
                           attrs="active_in_regimes = ['CRISIS']")
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_min_lookback_beyond_the_synthetic_panel_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzLongLookback', attrs='min_lookback = 400')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_emitting_strategy_gets_no_warning(tmp_path):
    path = _write_strategy(tmp_path, 'ZzEmitter', body=_EMITTING_BODY)
    res = vs.validate(path)
    assert res['ok'] is True, res['errors']
    assert res['signal_count'] == 3
    assert res['warnings'] == []


def test_manifest_eligible_regimes_without_low_vol_is_exempt(tmp_path, monkeypatch):
    """A strategy the operator already gated away from LOW_VOL in the manifest
    is exempt even when its class-level active_in_regimes still lists it."""
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(
        '{"strategies": {"zzmanifestgated": '
        '{"metadata": {"eligible_regimes": ["HIGH_VOL", "CRISIS"]}}}}'
    )
    monkeypatch.setattr(vs, 'MANIFEST_PATH', str(manifest))
    path = _write_strategy(tmp_path, 'ZzManifestGated')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_manifest_read_failure_does_not_exempt(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, 'MANIFEST_PATH', str(tmp_path / 'does_not_exist.json'))
    path = _write_strategy(tmp_path, 'ZzNoManifest')
    res = vs.validate(path)
    assert res['warnings'] == ['zero_signals_synthetic']


@pytest.mark.parametrize('manifest_body', [
    '[]',
    '{"strategies": []}',
    '{"strategies": {"zzbadshape": "oops"}}',
    '{"strategies": {"zzbadshape": {"metadata": "oops"}}}',
])
def test_manifest_bad_shape_does_not_crash_validation(tmp_path, monkeypatch, manifest_body):
    """A malformed manifest (list instead of dict, entry/metadata not a dict,
    etc.) must degrade to 'not exempt' — never raise. A crash here would turn
    a WARNING into a hard validate() failure, which spec §0 forbids: this
    feature must never change a verdict, let alone blow one up entirely."""
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(manifest_body)
    monkeypatch.setattr(vs, 'MANIFEST_PATH', str(manifest))
    path = _write_strategy(tmp_path, 'ZzBadShape')
    res = vs.validate(path)
    assert res['ok'] is True, res['errors']
    assert res['warnings'] == ['zero_signals_synthetic']


@pytest.mark.parametrize('n_closed,expected', [(0, True), (1, False)])
def test_exempt_helper_is_pure(n_closed, expected):
    """_zero_signal_exempt reads only class attributes — no I/O for the
    calendar_edge branch."""
    class _Fake:
        calendar_edge = bool(n_closed == 0)
        active_in_regimes = ['LOW_VOL']
        min_lookback = 20
        id = None
    assert vs._zero_signal_exempt(_Fake) is expected
