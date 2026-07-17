"""SP-5.1b-ii Task 6a — unit tests for _inject_option_hedge_targets.

All external I/O (DB cursor, _spot_price) is mocked. Gate-OFF
behaviour (equity path byte-identical) is verified by the regression
suite (test_sizer_sp6_eod_mode / test_sp6_classify_position_deltas).
"""
import sys
sys.path.insert(0, 'src')
from unittest.mock import patch


def test_inject_adds_hedge_on_top_post_normalization():
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    target = {'AAPL': 50000.0}            # equity, already normalized
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return [('SPY','SHORT',{'is_hedge':'true','hedge_shares':30.0})]
    with patch('execution.regime_blended_sizer._spot_price', return_value=760.0):
        out = _inject_option_hedge_targets(_Cur(), dict(target), {'equity':100000.0,'buying_power':400000.0})
    assert out['AAPL'] == 50000.0                  # equity byte-identical
    assert round(out['SPY'], 0) == round(-30*760.0, 0)  # hedge short, added on top


def test_inject_headroom_scales_when_exceeds_bp():
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return [('SPY','LONG',{'is_hedge':'true','hedge_shares':1000.0})]
    target = {'AAPL': 90000.0}
    with patch('execution.regime_blended_sizer._spot_price', return_value=760.0):
        out = _inject_option_hedge_targets(_Cur(), dict(target), {'equity':100000.0,'buying_power':100000.0})
    # equity_gross 90k, bp 100k -> headroom 10k; hedge gross 760k -> scaled to <=10k
    assert abs(out['SPY']) <= 10000.0 + 1.0 and out['AAPL'] == 90000.0


def test_inject_noop_when_no_hedge_rows():
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return []
    out = _inject_option_hedge_targets(_Cur(), {'AAPL':5.0}, {'equity':1.0})
    assert out == {'AAPL':5.0}


def test_inject_noop_when_no_headroom():
    """When buying_power <= equity_gross, hedge is skipped entirely."""
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return [('SPY','LONG',{'is_hedge':'true','hedge_shares':10.0})]
    target = {'AAPL': 100000.0}
    with patch('execution.regime_blended_sizer._spot_price', return_value=500.0):
        out = _inject_option_hedge_targets(_Cur(), dict(target), {'equity':100000.0,'buying_power':100000.0})
    # equity_gross == buying_power → headroom == 0 → hedge NOT injected
    assert 'SPY' not in out
    assert out['AAPL'] == 100000.0


def test_inject_skips_zero_hedge_shares():
    """Rows with hedge_shares=0 are silently skipped."""
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return [('SPY','LONG',{'is_hedge':'true','hedge_shares':0.0})]
    out = _inject_option_hedge_targets(_Cur(), {'AAPL':1000.0}, {'equity':10000.0,'buying_power':40000.0})
    assert 'SPY' not in out


def test_inject_skips_missing_spot_price():
    """If _spot_price returns None for a ticker, that hedge row is dropped."""
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return [('SPY','LONG',{'is_hedge':'true','hedge_shares':10.0})]
    with patch('execution.regime_blended_sizer._spot_price', return_value=None):
        out = _inject_option_hedge_targets(_Cur(), {'AAPL':1000.0}, {'equity':10000.0,'buying_power':40000.0})
    assert 'SPY' not in out


def test_inject_accumulates_multiple_hedge_rows_same_ticker():
    """Multiple hedge rows for the same underlying are summed."""
    from execution.regime_blended_sizer import _inject_option_hedge_targets
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self):
            return [
                ('SPY','LONG',{'is_hedge':'true','hedge_shares':10.0}),
                ('SPY','LONG',{'is_hedge':'true','hedge_shares':5.0}),
            ]
    with patch('execution.regime_blended_sizer._spot_price', return_value=100.0):
        out = _inject_option_hedge_targets(_Cur(), {'AAPL':50000.0}, {'equity':100000.0,'buying_power':400000.0})
    assert abs(out['SPY'] - 1500.0) < 0.01
