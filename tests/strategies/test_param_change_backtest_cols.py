import os, psycopg2, pytest
from strategies import eligibility_manager as em

URI = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(not URI, reason='needs DB')
def test_set_params_writes_backtest_cols():
    sid = '__test_couple__'
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("DELETE FROM strategy_regime_param_changes WHERE strategy_id=%s", (sid,))
        cur.execute("DELETE FROM strategy_regime_params WHERE strategy_id=%s", (sid,))
        c.commit()
    em.set_params(strategy_id=sid, regime_state='LOW_VOL', stop_pct=0.06,
                  actor='saturday_coupling', reason='test', source='saturday_coupling',
                  bt_sharpe_before=0.50, bt_sharpe_after=0.65, bt_n_trades=42)
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("""SELECT bt_sharpe_before, bt_sharpe_after, bt_n_trades, source
                       FROM strategy_regime_param_changes WHERE strategy_id=%s""", (sid,))
        row = cur.fetchone()
    assert row is not None
    assert float(row[0]) == 0.50 and float(row[1]) == 0.65 and row[2] == 42
    assert row[3] == 'saturday_coupling'

@pytest.mark.skipif(not URI, reason='needs DB')
def test_set_params_backtest_cols_default_null():
    sid = '__test_couple2__'
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("DELETE FROM strategy_regime_param_changes WHERE strategy_id=%s", (sid,))
        cur.execute("DELETE FROM strategy_regime_params WHERE strategy_id=%s", (sid,))
        c.commit()
    em.set_params(strategy_id=sid, regime_state='LOW_VOL', size_scalar=0.8,
                  actor='cli', reason='test')
    with psycopg2.connect(URI) as c, c.cursor() as cur:
        cur.execute("""SELECT bt_sharpe_before, bt_sharpe_after, bt_n_trades
                       FROM strategy_regime_param_changes WHERE strategy_id=%s""", (sid,))
        row = cur.fetchone()
    assert row == (None, None, None)
