"""JS/Python parity for the promotion + activation sleeve gate.

The candidate->live PROMOTION gate lives in JS (`src/lib/promotion_service.js`,
`PROMOTION_THRESHOLDS`), while the per-(strategy, regime) ACTIVATION gate lives
in Python and derives its numbers from `strategies.lifecycle.PROMOTION_THRESHOLDS`
via `backtest.regime_qualification.class_thresholds()`.

Operator policy: activation is promotion's gate PLUS the min-Sharpe slider on
top (`pipeline_config.strategy_activation_min_sharpe`) — so the two must agree
on every underlying threshold, including the 2026-07-27 Calmar escape hatch on
the drawdown leg. The JS side is a HAND-MAINTAINED mirror whose own header says
"keep in sync"; before this test, nothing enforced that. Editing lifecycle.py
moved activation automatically while promotion silently kept the old values,
which is precisely the drift that would make a strategy promotable but not
activatable (or worse, the reverse).

Verified in sync 2026-08-05; this test is what keeps it that way.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backtest.regime_qualification import class_thresholds, dd_leg_passes

JS_PATH = Path(__file__).resolve().parents[2] / 'src' / 'lib' / 'promotion_service.js'
CLASSES = ('equity', 'etp', 'option', 'crypto')

# JS key -> class_thresholds() key. The JS carries drawdowns already in PERCENT
# (max_drawdown_pct / dd_hard_cap_pct); lifecycle.py stores them as FRACTIONS
# and class_thresholds() scales by 100, so both sides compare in percent here.
KEY_MAP = {
    'min_sharpe':       'min_sharpe',
    'max_drawdown_pct': 'max_dd_pct',
    'min_trades':       'min_trades',
    'min_calmar':       'min_calmar',
    'dd_hard_cap_pct':  'dd_hard_cap_pct',
}


def _parse_js_thresholds() -> dict[str, dict[str, float]]:
    src = JS_PATH.read_text()
    try:
        block = src.split('const PROMOTION_THRESHOLDS = {', 1)[1].split('};', 1)[0]
    except IndexError:                                    # pragma: no cover
        pytest.fail(f'PROMOTION_THRESHOLDS literal not found in {JS_PATH}')
    out: dict[str, dict[str, float]] = {}
    for line in block.splitlines():
        m = re.match(r'\s*(\w+)\s*:\s*\{(.+?)\}', line)
        if not m:
            continue
        cls, body = m.group(1), m.group(2)
        out[cls] = {
            k.strip(): float(v)
            for k, v in (p.split(':', 1) for p in body.split(',') if ':' in p)
        }
    return out


def test_js_mirror_covers_every_class():
    js = _parse_js_thresholds()
    missing = [c for c in CLASSES if c not in js]
    assert not missing, f'promotion_service.js is missing classes: {missing}'


@pytest.mark.parametrize('cls', CLASSES)
def test_promotion_and_activation_thresholds_match(cls):
    js = _parse_js_thresholds()[cls]
    py = class_thresholds(cls)
    for js_key, py_key in KEY_MAP.items():
        assert js_key in js, f'{cls}: promotion_service.js lost key {js_key!r}'
        assert float(js[js_key]) == pytest.approx(float(py[py_key])), (
            f'{cls}.{js_key}: promotion(JS)={js[js_key]} != activation(PY)={py[py_key]}. '
            'lifecycle.PROMOTION_THRESHOLDS and promotion_service.js have drifted — '
            'the promotion gate and the activation gate now disagree.'
        )


def test_calmar_hatch_semantics_match_the_js_dd_leg():
    """dd <= ceiling OR (calmar >= min_calmar AND dd <= hard cap); missing
    calmar forfeits ONLY the hatch — never a silent pass. Mirrors
    judgeRegimeSleeve's ddOk in promotion_service.js."""
    thr = class_thresholds('equity')          # ceiling 20, calmar 0.5, cap 50
    assert dd_leg_passes(10.0, None, thr) is True     # under ceiling, hatch irrelevant
    assert dd_leg_passes(26.0, 0.8, thr) is True      # hatch: good calmar, under cap
    assert dd_leg_passes(26.0, 0.4, thr) is False     # calmar below floor
    assert dd_leg_passes(26.0, None, thr) is False    # missing calmar -> ceiling only
    assert dd_leg_passes(60.0, 5.0, thr) is False     # past the catastrophic cap
    assert dd_leg_passes(None, 5.0, thr) is False     # missing dd fails closed


def test_promotion_sharpe_floor_is_zero_and_strict():
    """Promotion is deliberately permissive (>0); the 0.5 slider is the risk
    dial applied on top at activation. If this ever becomes non-zero, the
    slider is no longer the sole dial and the operator model changes."""
    for cls in CLASSES:
        assert class_thresholds(cls)['min_sharpe'] == 0.0
