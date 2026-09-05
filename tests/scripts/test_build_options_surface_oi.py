# tests/scripts/test_build_options_surface_oi.py
from __future__ import annotations
import datetime as dt, importlib.util, json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def test_builder_writes_oi_scalars_from_cboe_session(tmp_path, monkeypatch):
    from strategies import options_oi as oi
    spec = importlib.util.spec_from_file_location('bos', ROOT / 'scripts' / 'build_options_surface.py')
    bos = importlib.util.module_from_spec(spec); spec.loader.exec_module(bos)
    root = tmp_path / 'cboe_chains'; root.mkdir()
    rows = [{'date': dt.date(2026, 9, 2), 'underlying': 'SPY', 'expiry': dt.date(2026, 9, 18), 'option_type': t,
             'strike': k, 'open_interest': o, 'iv': 0.12, 'delta': d, 'gamma': 0.01, 'vega': 0.5, 'underlying_price': 640.0}
            for k, o, d, t in ((630.0, 1000.0, 0.6, 'C'), (640.0, 2000.0, 0.5, 'C'), (650.0, 500.0, 0.4, 'C'),
                               (630.0, 900.0, -0.4, 'P'), (640.0, 2200.0, -0.5, 'P'), (650.0, 300.0, -0.6, 'P'))]
    pd.DataFrame(rows).to_parquet(root / 'date=2026-09-02.parquet', index=False)
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(root)); oi.clear_cache()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet'); chain['date'] = pd.to_datetime(chain['date'])
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    out = bos.build_rows(chain, {(t, pd.Timestamp('2026-09-03')): s for t, s in spots.items()}, oi.oi_lookup_factory())
    spy = out[out.ticker == 'SPY'].iloc[0]
    assert spy['oi_session'] == '2026-09-02' and spy['gex'] is not None and spy['max_pain'] == 640.0
    assert out[out.ticker == 'AAPL'].iloc[0]['gex'] is None      # no CBOE rows for AAPL in this fixture
