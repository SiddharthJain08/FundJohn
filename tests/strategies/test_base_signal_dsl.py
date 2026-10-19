"""C7b: BaseStrategy SIGNAL integration (class-definition validation + default generate_signals)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.base import BaseStrategy, CANONICAL_REGIMES

REG = {'state': 'LOW_VOL'}


def _mk(**attrs):
    body = {'id': 'ZZTEST_dsl', 'active_in_regimes': list(CANONICAL_REGIMES), 'SIGNAL': 'close - 100'}
    body.update(attrs)
    return type('Dyn', (BaseStrategy,), body)


def _prices(last, n=30):
    idx = pd.bdate_range('2024-01-01', periods=n)
    return pd.DataFrame({t: np.r_[np.full(n - 1, 100.0), v] for t, v in last.items()}, index=idx)


def _run(cls, prices, regime=REG, universe=None):
    return cls().generate_signals(prices, regime, universe or list(prices.columns))


def test_dsl_class_is_concrete_and_instantiates():
    cls = _mk()
    assert cls.__abstractmethods__ == frozenset()
    assert cls().id == 'ZZTEST_dsl'


def test_adapters_stay_abstract():
    from strategies.oxford_crabel import OxfordBaseStrategy
    from strategies.cohort_base import CohortBaseStrategy
    assert 'generate_signals' in OxfordBaseStrategy.__abstractmethods__
    # Cohort implements generate_signals itself and stays abstract on its own hook.
    assert '_generate_signals_cohort' in CohortBaseStrategy.__abstractmethods__
    assert CohortBaseStrategy.generate_signals is not BaseStrategy._signal_dsl_generate
    assert 'generate_signals' in BaseStrategy.__abstractmethods__


def test_bad_signal_fails_at_class_definition():
    with pytest.raises(TypeError, match="unknown symbol 'vol'"):
        _mk(SIGNAL='close / vol')
    with pytest.raises(TypeError, match='ZZ'):
        type('ZZ', (BaseStrategy,), {'SIGNAL': "__import__('os')"})


@pytest.mark.parametrize('attrs', [
    {'SIGNAL_DIRECTION': 'up'},
    {'SIGNAL_THRESHOLD': (0.0, 1.0)},
    {'SIGNAL_THRESHOLD': 'x'},
    {'SIGNAL_DIRECTION': 'both', 'SIGNAL_THRESHOLD': 0.0},
    {'SIGNAL_DIRECTION': 'both', 'SIGNAL_THRESHOLD': (1.0, 1.0)},
    {'SIGNAL_DIRECTION': 'both', 'SIGNAL_THRESHOLD': (2.0, 1.0)},
    {'SIGNAL_COLUMNS': frozenset({'volume'})},
])
def test_bad_direction_threshold_or_missing_close_fail_at_definition(attrs):
    with pytest.raises(TypeError, match='SIGNAL'):
        _mk(**attrs)


def test_extra_columns_require_signal_panel_override():
    with pytest.raises(TypeError, match='signal_panel'):
        _mk(SIGNAL='close * volume', SIGNAL_COLUMNS=frozenset({'close', 'volume'}))

    def sp(self, prices, universe, aux_data):
        from strategies.signal_expr import make_panel
        return make_panel({'close': prices, 'volume': prices * 0 + 2.0})
    cls = _mk(SIGNAL='close * volume', SIGNAL_COLUMNS=frozenset({'close', 'volume'}),
              signal_panel=sp, SIGNAL_THRESHOLD=100.0)
    assert [s.ticker for s in _run(cls, _prices({'ZZTEST1': 150.0, 'ZZTEST2': 50.0}))] == ['ZZTEST1']


def test_min_lookback_auto_raised():
    assert _mk(SIGNAL='sma(close, 200)', min_lookback=20).min_lookback == 201
    assert _mk(SIGNAL='sma(close, 200)', min_lookback=504).min_lookback == 504


def test_long_threshold_is_strict():
    cls = _mk(SIGNAL_THRESHOLD=0.0)
    out = _run(cls, _prices({'ZZTEST1': 100.0, 'ZZTEST2': 101.0}))
    assert [s.ticker for s in out] == ['ZZTEST2']


def test_short_and_both_directions():
    p = _prices({'ZZTEST1': 90.0, 'ZZTEST2': 110.0, 'ZZTEST3': 100.0})
    s = _run(_mk(SIGNAL_DIRECTION='short'), p)
    assert [(x.ticker, x.direction) for x in s] == [('ZZTEST1', 'SHORT')]
    b = _run(_mk(SIGNAL_DIRECTION='both', SIGNAL_THRESHOLD=(-5.0, 5.0)), p)
    assert [(x.ticker, x.direction) for x in b] == [('ZZTEST1', 'SHORT'), ('ZZTEST2', 'LONG')]
    # boundary values are strict for both legs
    b2 = _run(_mk(SIGNAL_DIRECTION='both', SIGNAL_THRESHOLD=(-10.0, 10.0)), p)
    assert b2 == []


def test_max_signals_cap_and_tie_order():
    p = _prices({'ZZTEST3': 110.0, 'ZZTEST1': 110.0, 'ZZTEST2': 110.0, 'ZZTEST4': 120.0})
    out = _run(_mk(MAX_SIGNALS=3), p)
    assert [s.ticker for s in out] == ['ZZTEST4', 'ZZTEST1', 'ZZTEST2']


def test_explicit_generate_signals_wins():
    marker = []

    def gs(self, prices, regime, universe, aux_data=None):
        marker.append(1)
        return []
    cls = _mk(generate_signals=gs)
    assert cls.generate_signals is gs
    _run(cls, _prices({'ZZTEST1': 150.0}))
    assert marker == [1]


def test_signal_fields():
    p = _prices({'ZZTEST1': 120.0, 'ZZTEST2': 80.0})
    cls = _mk(SIGNAL_DIRECTION='both', SIGNAL_THRESHOLD=(-5.0, 5.0))
    out = {s.ticker: s for s in _run(cls, p)}
    lg, sh = out['ZZTEST1'], out['ZZTEST2']
    assert lg.entry_price == 120.0 and lg.direction == 'LONG'
    assert lg.stop_loss < lg.entry_price < lg.target_1
    assert sh.stop_loss > sh.entry_price > sh.target_1
    assert set(lg.signal_params) == {'dsl_value', 'formula_hash', 'regime'}
    assert lg.signal_params['dsl_value'] == 20.0 and lg.signal_params['regime'] == 'LOW_VOL'
    assert lg.signal_params['formula_hash'] == cls._compiled_signal.formula_hash
    assert lg.position_size_pct == 0.05 and lg.confidence == 'MED'


def _dsl_lines(err):
    return [ln for ln in err.splitlines() if ln.startswith('[signal_dsl]')]


def test_weekend_last_row_emits_nothing_but_logs(capsys):
    p = _prices({'ZZTEST1': 150.0, 'ZZTEST2': 150.0})
    p.loc[p.index[-1] + pd.Timedelta(days=1)] = np.nan
    assert _run(_mk(), p) == []
    err = capsys.readouterr().err
    lines = _dsl_lines(err)
    assert len(lines) == 1 and 'values=0' in lines[0] and 'skip=' not in lines[0]
    assert '[debug] signals=0' in err


def test_signal_dsl_line_on_every_skip_path(capsys):
    p = _prices({'ZZTEST1': 150.0})

    def one(cls, prices, regime=REG, universe=None):
        capsys.readouterr()
        assert _run(cls, prices, regime, universe) == []
        lines = _dsl_lines(capsys.readouterr().err)
        assert len(lines) == 1
        return lines[0]
    assert 'skip=regime' in one(_mk(active_in_regimes=['CRISIS']), p)
    assert 'skip=gate' in one(_mk(dsl_should_fire=lambda self, prices, regime: False), p)
    assert 'skip=lookback' in one(_mk(SIGNAL='sma(close, 100)'), p)
    assert 'skip=panel_empty' in one(_mk(), p, universe=['NOPE'])
    # empty prices: nothing to log
    capsys.readouterr()
    assert _run(_mk(), p.iloc[0:0]) == []
    assert _dsl_lines(capsys.readouterr().err) == []


def test_no_existing_implementation_declares_signal():
    import importlib, pkgutil
    import strategies.implementations as impl
    for m in pkgutil.iter_modules(impl.__path__):
        try:
            mod = importlib.import_module(f'strategies.implementations.{m.name}')
        except Exception:
            continue
        for v in vars(mod).values():
            if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                assert not v.__dict__.get('SIGNAL'), (m.name, v)
