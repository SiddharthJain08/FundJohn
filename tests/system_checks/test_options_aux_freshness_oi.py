from __future__ import annotations
import datetime as dt
import pandas as pd

from system_checks.checks import options_aux_freshness as chk
from system_checks.types import Status


def test_oi_coverage_guard(tmp_path, monkeypatch):
    panel = tmp_path / 'enriched.parquet'
    today = pd.Timestamp.today().normalize()
    df = pd.DataFrame({'ticker': [f'T{i}' for i in range(500)], 'date': today, 'gex': None,
                       'contracts_liquid': None, 'iv_centroid_delta': None, 'surface_premium': None,
                       'pcr_oi': [None] * 300 + [1.0] * 200})
    df.to_parquet(panel, index=False)
    monkeypatch.setenv('OPTIONS_ENRICHED_PANEL', str(panel))
    root = tmp_path / 'cboe_chains'; root.mkdir()
    pd.DataFrame([{'underlying': 'T1'}]).to_parquet(root / f'date={(today - pd.Timedelta(days=1)).date()}.parquet', index=False)
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(root))
    status, msg = chk.oi_coverage(min_tickers=400)
    assert status == Status.FAIL and '200' in msg
    df['pcr_oi'] = 1.0; df.to_parquet(panel, index=False)
    assert chk.oi_coverage(min_tickers=400)[0] == Status.PASS
