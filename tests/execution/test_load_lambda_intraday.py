# tests/test_load_lambda_intraday.py — the intraday λ key is RETIRED:
# _load_lambda reads position_sizing_lambda for EVERY lane (operator
# single-λ ruling 2026-08-12). History: the stale 1× value in
# position_sizing_lambda_intraday sized the whole 2026-08-12 intraday
# redeploy at half the intended gross. These tests pin the collapse.
import inspect
import os

import pytest

try:
    import psycopg2
except ImportError:
    psycopg2 = None


def test_load_lambda_reads_the_single_global_key():
    dsn = os.environ.get("POSTGRES_URI")
    if dsn is None:
        pytest.skip("POSTGRES_URI not set")
    if psycopg2 is None:
        pytest.skip("psycopg2 not installed")
    from src.execution.regime_blended_sizer import _load_lambda
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT value FROM pipeline_config WHERE key='position_sizing_lambda'")
        r = cur.fetchone()
    expect = max(0.10, min(2.00, float(r[0]))) if r else 2.0
    assert _load_lambda() == expect


def test_load_lambda_has_no_intraday_parameter():
    # The retired branch must not quietly return: a caller passing intraday=
    # should fail loudly (TypeError), never silently size at a stale value.
    from src.execution.regime_blended_sizer import _load_lambda
    assert 'intraday' not in inspect.signature(_load_lambda).parameters
    with pytest.raises(TypeError):
        _load_lambda(intraday=True)
