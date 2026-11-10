"""Crypto engine Phase 3, Task 7 — pure helpers in execution.pooled_sizing."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import pooled_sizing as ps  # noqa: E402


def test_pooled_sizing_enabled_only_on_1():
    assert ps.pooled_sizing_enabled({'OPENCLAW_CRYPTO_POOLED_SIZING': '1'})
    assert not ps.pooled_sizing_enabled({'OPENCLAW_CRYPTO_POOLED_SIZING': '0'})
    assert not ps.pooled_sizing_enabled({})


def test_ticker_classes_crypto_if_any_contributor_is_crypto():
    meta = {'BTC-USD': {'strategies': ['S_beta_btc']},
            'MIX': {'strategies': ['S_eq', 'S_cr']},
            'AAPL': {'strategies': ['S_eq', 'S_beta_spy']}}
    cls = {'S_beta_btc': 'crypto', 'S_cr': 'crypto', 'S_beta_spy': 'etp'}
    out = ps.ticker_classes(meta, lambda s: cls.get(s, 'equity'))
    assert out == {'BTC-USD': 'crypto', 'MIX': 'crypto', 'AAPL': 'equity'}   # 'etp' is equity


def test_split_by_class():
    eq, cr = ps.split_by_class({'A': 1.0, 'BTC-USD': 2.0, 'Z': 3.0}, {'BTC-USD': 'crypto', 'A': 'equity'})
    assert eq == {'A': 1.0, 'Z': 3.0} and cr == {'BTC-USD': 2.0}


def test_merge_weights_crypto_ids_take_crypto_row():
    eq = [{'strategy_id': 'S_eq', 'daily_weight': 1.0}, {'strategy_id': 'S_cr', 'daily_weight': 9.0}]
    cr = [{'strategy_id': 'S_cr', 'daily_weight': 2.0}, {'strategy_id': 'S_eq', 'daily_weight': 7.0},
          {'strategy_id': 'S_cr2', 'daily_weight': 3.0}]
    out = {r['strategy_id']: r['daily_weight'] for r in ps.merge_weights(eq, cr, {'S_cr', 'S_cr2'})}
    assert out == {'S_eq': 1.0, 'S_cr': 2.0, 'S_cr2': 3.0}


def test_crypto_gross_cap_pro_rata():
    t, shaved = ps.crypto_gross_cap({'BTC-USD': 40_000.0, 'ETH-USD': -20_000.0}, 100_000.0, 0.30)
    assert abs(sum(abs(v) for v in t.values()) - 30_000.0) < 1e-6
    assert abs(t['BTC-USD'] - 20_000.0) < 1e-6 and abs(t['ETH-USD'] + 10_000.0) < 1e-6
    assert abs(shaved - 30_000.0) < 1e-6


def test_crypto_gross_cap_noop_under_cap():
    src = {'BTC-USD': 10_000.0}
    t, shaved = ps.crypto_gross_cap(src, 100_000.0, 0.5)
    assert t == src and shaved == 0.0 and t is not src


def test_funding_pass_scales_when_over_nmbp():
    t, short = ps.funding_pass({'BTC-USD': 30_000.0, 'SOL-USD': 10_000.0}, 20_000.0)
    assert abs(t['BTC-USD'] - 15_000.0) < 1e-6 and abs(t['SOL-USD'] - 5_000.0) < 1e-6
    assert abs(short - 20_000.0) < 1e-6


def test_funding_pass_unknown_and_nonbinding():
    src = {'BTC-USD': 30_000.0}
    t, short = ps.funding_pass(src, None)
    assert t == src and short is None
    t, short = ps.funding_pass(src, 50_000.0)
    assert t == src and short == 0.0
    t, short = ps.funding_pass(src, -5.0)          # negative nmbp -> nothing fundable
    assert t == {'BTC-USD': 0.0} and abs(short - 30_000.0) < 1e-6


def test_pooled_scales_reduce_to_equity_scale_without_crypto():
    s_eq, s_cr = ps.pooled_scales(eq_gross=4.0, cr_gross=0.0, lam_global=2.0,
                                  liq_eq=0.75, liq_cr=1.0, nav=100_000.0)
    assert s_eq == (2.0 * 0.75 * 100_000.0) / 4.0


def test_pooled_scales_share_one_normaliser():
    s_eq, s_cr = ps.pooled_scales(eq_gross=3.0, cr_gross=1.0, lam_global=2.0,
                                  liq_eq=1.0, liq_cr=0.5, nav=100_000.0)
    assert abs(s_eq - 200_000.0 / 4.0) < 1e-9 and abs(s_cr - 100_000.0 / 4.0) < 1e-9


def test_drop_stables():
    kept, n = ps.drop_stables({'BTC-USD': 1.0, 'USDC-USD': 2.0, 'USDT-USD': 3.0}, {'USDC-USD', 'USDT-USD'})
    assert kept == {'BTC-USD': 1.0} and n == 2


def test_stable_tickers_reads_universe_roles():
    s = ps.stable_tickers()
    assert 'USDC-USD' in s and 'BTC-USD' not in s


def test_apply_hold_flat_sleeve_zeroes_all_ticker_hold_zeroes_one():
    t = {'BTC-USD': 10.0, 'ETH-USD': 5.0}
    out, n = ps.apply_hold_flat(t, {'sleeve': {'since': 'x'}, 'tickers': {}})
    assert out == {'BTC-USD': 0.0, 'ETH-USD': 0.0} and n == 2
    out, n = ps.apply_hold_flat(t, {'sleeve': None, 'tickers': {'ETH-USD': {'since': 'x'}}})
    assert out == {'BTC-USD': 10.0, 'ETH-USD': 0.0} and n == 1
    out, n = ps.apply_hold_flat(t, {'sleeve': None, 'tickers': {}})
    assert out == t and n == 0


def test_shadow_line_format():
    line = ps.shadow_line('CRISIS', -0.4, {'BTC-USD': 30_000.0, 'ETH-USD': 20_000.0},
                          {'BTC-USD': 25_000.0, 'ETH-USD': 15_000.0}, ['SOL-USD'], {'BTC-USD'},
                          200_000.0, gross_cap_shaved=10_000.0, funding_shortfall=None,
                          hold_flat=False, mode='shadow', nav=100_000.0, stable_excluded=2)
    assert line.startswith('crypto_sizing.shadow[CRISIS]: S_m=-0.40 ')
    for tok in ('n=2', 'gross=40000', 'unconstrained=50000', 'cap_shaved=10000',
                'funding_bind=unknown', 'hold=0', 'stable_excluded=2', 'dropped=1', "bench=['BTC-USD']"):
        assert tok in line, tok
    line2 = ps.shadow_line('LOW_VOL', None, {}, {}, [], set(), 0.0, gross_cap_shaved=0.0,
                           funding_shortfall=1234.4, hold_flat=True, mode='apply')
    assert line2.startswith('crypto_sizing.apply[LOW_VOL]: S_m=na n=0 ')
    assert 'funding_bind=1234' in line2 and 'hold=1' in line2


class _Cur:
    def __init__(self, row): self.row, self.sql = row, None
    def execute(self, sql, args=None): self.sql, self.args = sql, args
    def fetchone(self): return self.row
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, row): self.cur = _Cur(row)
    def cursor(self, *a, **k): return self.cur


def test_load_crypto_regime_and_params(monkeypatch):
    import regime.crypto_regime as cr
    monkeypatch.setattr(cr, 'load_crypto_regime_state', lambda path=None: {'state': 'CRISIS'})
    conn = _Conn({'regime_state': 'CRISIS', 'liquidity_param': 0.5, 'min_acting_strategies': 2})
    state, params = ps.load_crypto_regime_and_params(conn)
    assert state == 'CRISIS' and params['liquidity_param'] == 0.5
    assert conn.cur.args == ('CRISIS',) and 'regime_sizer_params' in conn.cur.sql


def test_load_crypto_regime_and_params_fail_open(monkeypatch):
    import regime.crypto_regime as cr
    monkeypatch.setattr(cr, 'load_crypto_regime_state', lambda path=None: {'state': None})
    assert ps.load_crypto_regime_and_params(_Conn(None)) == (None, None)
    monkeypatch.setattr(cr, 'load_crypto_regime_state', lambda path=None: {'state': 'LOW_VOL'})
    state, params = ps.load_crypto_regime_and_params(_Conn(None))      # no params row -> defaults
    assert state == 'LOW_VOL' and params == {'liquidity_param': 1.0}
    def boom(path=None): raise RuntimeError('x')
    monkeypatch.setattr(cr, 'load_crypto_regime_state', boom)
    assert ps.load_crypto_regime_and_params(_Conn(None)) == (None, None)
