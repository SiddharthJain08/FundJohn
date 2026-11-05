"""run_evening_marks._closes_for must honour the run date (regression for the
2026-10-30 backfill that stamped the 10-29 close onto 10-23/10-26/10-28 rows)."""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        'run_evening_marks_under_test', ROOT / 'scripts' / 'run_evening_marks.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _panel():
    idx = pd.to_datetime(['2026-10-23', '2026-10-26', '2026-10-28', '2026-10-29'])
    return pd.DataFrame({'AAA_T': [1.0, 2.0, 3.0, 4.0],
                         'BBB_T': [10.0, None, 30.0, 40.0]}, index=idx)


def test_closes_as_of_truncates_to_run_date(monkeypatch):
    mod = _load_module()
    import execution.engine as eng
    monkeypatch.setattr(eng, 'load_prices', lambda universe, **kw: _panel())
    closes = mod._closes_for(as_of=date(2026, 10, 26))
    assert closes == {'AAA_T': 2.0, 'BBB_T': 10.0}   # BBB has no 10-26 row -> last prior close


def test_closes_without_as_of_keeps_latest(monkeypatch):
    mod = _load_module()
    import execution.engine as eng
    monkeypatch.setattr(eng, 'load_prices', lambda universe, **kw: _panel())
    assert mod._closes_for() == {'AAA_T': 4.0, 'BBB_T': 40.0}


def test_main_passes_run_date_to_closes():
    src = (ROOT / 'scripts' / 'run_evening_marks.py').read_text()
    assert 'closes = _closes_for(as_of=run_date)' in src
