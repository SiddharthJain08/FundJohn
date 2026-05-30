import os, psycopg2, pytest
DSN = os.environ.get("POSTGRES_URI")

@pytest.mark.integration
def test_panel_table_and_columns_exist():
    assert DSN, "POSTGRES_URI required"
    conn = psycopg2.connect(DSN); cur = conn.cursor()
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='strategy_backtest_panel'""")
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    expected = {'strategy_id','run_id','effective_sharpe','cadence_days',
                'oue_over','oue_under','oue_expected','oue_by_regime',
                'oue_sigma_gate','equity_curve','n_trades','computed_at'}
    assert expected <= cols, f"missing: {expected - cols}"
