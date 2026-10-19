"""prices_rowgroup_span check: registered, maps verdict to Status, SKIPs missing."""
import pandas as pd

from src.data import parquet_store as ps
from src.system_checks import run_one
from src.system_checks.checks import parquet_layout as chk
from src.system_checks.types import Status


def test_check_registered_and_maps_status(monkeypatch, tmp_path):
    monkeypatch.setattr(chk, '_MASTER_DIR', tmp_path)
    assert run_one('prices_rowgroup_span').status is Status.SKIP

    dates = pd.bdate_range('2024-01-01', periods=400)
    df = pd.DataFrame([(d, t, 1.0) for t in ['ZZTEST1', 'ZZTEST2', 'ZZTEST3']
                       for d in dates], columns=['date', 'ticker', 'close'])
    p = tmp_path / 'prices.parquet'
    ps._atomic_write(p, df, row_group_size=100)  # ticker-contiguous
    res = run_one('prices_rowgroup_span')
    assert res.status is Status.FAIL

    ps._atomic_write(p, df.sort_values(['date', 'ticker']), row_group_size=100)
    assert run_one('prices_rowgroup_span').status is Status.PASS
