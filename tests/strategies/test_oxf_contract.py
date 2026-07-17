"""Generic contract every oxf_* strategy must satisfy on a synthetic panel."""
import importlib, pkgutil, pandas as pd, numpy as np, pytest
import strategies.implementations as impl
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, OXFORD_ETF_BASKET

def _oxf_classes():
    out = []
    for m in pkgutil.iter_modules(impl.__path__):
        if not m.name.startswith('oxf_'):
            continue
        mod = importlib.import_module(f'strategies.implementations.{m.name}')
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, OxfordBaseStrategy)
                    and obj is not OxfordBaseStrategy):
                out.append(obj)
    return out

def _fake_close_panel(days=400):
    idx = pd.date_range('2021-01-04', periods=days, freq='B')
    rng = np.random.default_rng(0)
    data = {t: 100*np.cumprod(1+rng.normal(0.0003,0.012,days)) for t in OXFORD_ETF_BASKET}
    return pd.DataFrame(data, index=idx)

@pytest.mark.parametrize('cls', _oxf_classes(), ids=lambda c: c.id)
def test_contract(cls):
    s = cls()
    panel = _fake_close_panel()
    for regime in ('LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'):
        sigs = s.generate_signals(panel, {'state': regime}, list(panel.columns))
        assert isinstance(sigs, list)
        assert len(sigs) <= s.MAX_SIGNALS
        for sg in sigs:
            assert isinstance(sg, Signal)
            assert sg.direction in ('LONG','SHORT')
            assert sg.ticker in OXFORD_ETF_BASKET
            assert sg.entry_price > 0
            if sg.direction == 'LONG':
                assert sg.stop_loss < sg.entry_price < sg.target_1
            else:
                assert sg.target_1 < sg.entry_price < sg.stop_loss

@pytest.mark.parametrize('cls', _oxf_classes(), ids=lambda c: c.id)
def test_does_not_depend_on_universe_arg(cls):
    """Strategies must iterate the self-loaded basket, NOT the `universe` arg
    (the backtest may pass an sp500-scoped list that excludes ETFs). Passing an
    EMPTY universe must yield the same emitted tickers as a full one."""
    s = cls(); panel = _fake_close_panel(); reg = {'state': 'LOW_VOL'}
    empty = {sg.ticker for sg in s.generate_signals(panel, reg, [])}
    full  = {sg.ticker for sg in cls().generate_signals(panel, reg, list(panel.columns))}
    assert empty == full, f'{cls.id} changed output based on the universe arg'
