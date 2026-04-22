"""
validate_strategy.py — Contract validation harness for generated strategies.

Usage:
    python3 src/strategies/validate_strategy.py src/strategies/implementations/S_xx_foo.py

Exit code 0 = valid. Exit code 1 = invalid.
Prints JSON: {"ok": bool, "errors": [...], "signal_count": int}
"""

import sys
import os
import json
import importlib.util
import traceback
import inspect

import pandas as pd
import numpy as np


ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR  = os.path.join(ROOT, 'src')
sys.path.insert(0, ROOT)
sys.path.insert(0, SRC_DIR)


def _make_synthetic_prices(tickers=None, n_days=60) -> pd.DataFrame:
    """5 tickers × 60 trading days of synthetic OHLCV — just closes column."""
    if tickers is None:
        tickers = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'SPY']
    np.random.seed(42)
    idx = pd.bdate_range(end='2026-01-01', periods=n_days)
    data = {}
    for t in tickers:
        start = np.random.uniform(50, 500)
        returns = np.random.normal(0.0003, 0.015, n_days)
        prices = start * np.exp(np.cumsum(returns))
        data[t] = prices
    return pd.DataFrame(data, index=idx)


def _make_synthetic_regime() -> dict:
    return {
        'state':               'LOW_VOL',
        'state_probabilities': {'LOW_VOL': 1.0, 'TRANSITIONING': 0.0, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'confidence':          1.0,
        'transition_probs_tomorrow': {'LOW_VOL': 0.9, 'TRANSITIONING': 0.1, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'stress_score':        15,
        'roro_score':          40.0,
        'features':            {'vix': 14.0, 'vix_5d_chg': -0.5, 'vix_term_slope': 1.2,
                                'spx_rv_20d': 10.0, 'hy_ig_spread': 0.01, 'spx_5d_return': 0.02},
        'regime_change_alert': False,
        'days_in_current_state': 20,
        'position_scale':      1.0,
    }


def validate(filepath: str) -> dict:
    errors = []
    signal_count = 0

    # ── 1. File exists ────────────────────────────────────────────────────────
    if not os.path.isfile(filepath):
        return {'ok': False, 'errors': [f'File not found: {filepath}'], 'signal_count': 0}

    # ── 2. Syntax / import ────────────────────────────────────────────────────
    # Derive module path from file path so relative imports work correctly.
    # e.g. .../src/strategies/implementations/foo.py → strategies.implementations.foo
    abs_path = os.path.abspath(filepath)
    module_name = None
    if SRC_DIR in abs_path:
        rel = os.path.relpath(abs_path, SRC_DIR).replace(os.sep, '.')
        if rel.endswith('.py'):
            module_name = rel[:-3]
    if module_name:
        try:
            # Remove any cached version so re-runs don't get stale module
            sys.modules.pop(module_name, None)
            module = importlib.import_module(module_name)
        except Exception as e:
            return {'ok': False, 'errors': [f'Import error: {e}', traceback.format_exc()], 'signal_count': 0}
    else:
        # Fallback: direct file load (generated strategies with absolute imports)
        spec = importlib.util.spec_from_file_location('_strat_under_test', filepath)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return {'ok': False, 'errors': [f'Import error: {e}', traceback.format_exc()], 'signal_count': 0}

    # ── 3. Find BaseStrategy subclass ─────────────────────────────────────────
    from strategies.base import BaseStrategy, Signal

    def _is_strategy_class(obj):
        if not inspect.isclass(obj) or obj.__name__ == 'BaseStrategy':
            return False
        # Accept if: proper issubclass (canonical import) OR any base class is named BaseStrategy
        try:
            if issubclass(obj, BaseStrategy):
                return True
        except TypeError:
            pass
        return any(b.__name__ == 'BaseStrategy' for b in obj.__mro__[1:])

    strategy_classes = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if _is_strategy_class(obj)
    ]
    if not strategy_classes:
        return {'ok': False, 'errors': ['No BaseStrategy subclass found in file'], 'signal_count': 0}

    cls = strategy_classes[0]

    # ── 4a. Regime-tag check ──────────────────────────────────────────────────
    # The HMM classifier only emits LOW_VOL/TRANSITIONING/HIGH_VOL/CRISIS; any
    # other tag in active_in_regimes either needs to be a known synonym
    # (auto-normalized by BaseStrategy.__init_subclass__) or is a typo that
    # would make the strategy silently inert. Reject typos at the gate.
    from strategies.base import CANONICAL_REGIMES, REGIME_SYNONYMS
    # Inspect the author's original declaration (preserved by BaseStrategy)
    # rather than the runtime-normalized list, so typos like 'BOGUS_REGIME'
    # can't sneak through just because __init_subclass__ silently dropped them.
    _raw_tags = getattr(cls, '_raw_active_in_regimes', None) or getattr(cls, 'active_in_regimes', None) or []
    _unknown = [t for t in _raw_tags if t not in CANONICAL_REGIMES and t not in REGIME_SYNONYMS]
    if _unknown:
        errors.append(
            f'active_in_regimes contains unknown regime tag(s) {_unknown}. '
            f'Canonical set: {list(CANONICAL_REGIMES)}. '
            f'Known synonyms (auto-expanded at runtime): {list(REGIME_SYNONYMS.keys())}.'
        )

    # ── 4. Signature check ────────────────────────────────────────────────────
    try:
        sig = inspect.signature(cls.generate_signals)
        params = list(sig.parameters.keys())
        # Expected: self, prices, regime, universe[, aux_data]
        if len(params) < 4:
            errors.append(
                f'generate_signals has {len(params)} params ({params}) — '
                'expected (self, prices, regime, universe, aux_data=None)'
            )
    except Exception as e:
        errors.append(f'Could not inspect generate_signals signature: {e}')

    # ── 5. Empty DataFrame guard ──────────────────────────────────────────────
    try:
        instance = cls()
        # Pass empty-but-shaped aux_data so options strategies don't crash on
        # `aux_data.get(...)`. Strategies must still return [] for empty inputs.
        try:
            result = instance.generate_signals(pd.DataFrame(), {}, [], aux_data={'options': {}})
        except TypeError:
            result = instance.generate_signals(pd.DataFrame(), {}, [])
        if result is None:
            errors.append('generate_signals(empty) returned None — must return []')
        elif not isinstance(result, list):
            errors.append(f'generate_signals(empty) returned {type(result).__name__} — must return list')
        elif len(result) != 0:
            errors.append(f'generate_signals(empty) returned {len(result)} signals — must return []')
    except Exception as e:
        errors.append(f'generate_signals(empty df) raised: {e}')

    if errors:
        return {'ok': False, 'errors': errors, 'signal_count': 0}

    # ── 6. Synthetic run ──────────────────────────────────────────────────────
    prices  = _make_synthetic_prices()
    regime  = _make_synthetic_regime()
    universe = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA']

    try:
        instance = cls()
        # Pass an empty aux_data panel so options strategies that require it
        # in their signature (aux_data, not aux_data=None) still validate.
        # Strategies that don't accept aux_data are called the old way via fallback.
        try:
            signals = instance.generate_signals(prices, regime, universe, aux_data={'options': {}})
        except TypeError:
            signals = instance.generate_signals(prices, regime, universe)
    except Exception as e:
        return {'ok': False, 'errors': [f'generate_signals raised on synthetic data: {e}', traceback.format_exc()], 'signal_count': 0}

    if signals is None or not isinstance(signals, list):
        return {'ok': False, 'errors': ['generate_signals returned None or non-list'], 'signal_count': 0}

    signal_count = len(signals)

    # ── 7. Signal field type checks ───────────────────────────────────────────
    VALID_DIRECTIONS  = {'LONG', 'SHORT', 'SELL_VOL', 'BUY_VOL', 'FLAT'}
    VALID_CONFIDENCES = {'HIGH', 'MED', 'LOW'}

    for i, s in enumerate(signals[:10]):  # spot-check first 10
        if not isinstance(s, Signal):
            errors.append(f'signals[{i}] is {type(s).__name__}, not Signal')
            continue
        if not isinstance(s.ticker, str):
            errors.append(f'signals[{i}].ticker must be str, got {type(s.ticker).__name__}')
        if s.direction not in VALID_DIRECTIONS:
            errors.append(f'signals[{i}].direction={s.direction!r} not in {VALID_DIRECTIONS}')
        if not isinstance(s.confidence, str):
            errors.append(f'signals[{i}].confidence must be str (HIGH/MED/LOW), got {type(s.confidence).__name__}={s.confidence!r}')
        elif s.confidence not in VALID_CONFIDENCES:
            errors.append(f'signals[{i}].confidence={s.confidence!r} not in {VALID_CONFIDENCES}')
        for field in ('entry_price', 'stop_loss', 'target_1', 'target_2', 'target_3', 'position_size_pct'):
            val = getattr(s, field, None)
            if val is not None and not isinstance(val, (int, float)):
                errors.append(f'signals[{i}].{field} must be float, got {type(val).__name__}')
            if val is not None and (isinstance(val, float) and np.isnan(val)):
                errors.append(f'signals[{i}].{field} is NaN')

    ok = len(errors) == 0
    return {'ok': ok, 'errors': errors, 'signal_count': signal_count}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 validate_strategy.py <path/to/strategy.py>', file=sys.stderr)
        sys.exit(1)

    result = validate(sys.argv[1])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['ok'] else 1)
