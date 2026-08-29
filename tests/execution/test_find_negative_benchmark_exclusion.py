"""Amendment 1 D-D2: benchmark sleeves are never auto-demote candidates."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_weights as sw  # noqa: E402


class _Cur:
    def __init__(self, sleeve_ids):
        self.sleeve_ids = sleeve_ids
        self._rows = []
    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        if 'strategy_registry' in s:
            self._rows = [(i,) for i in self.sleeve_ids]
        else:
            self._rows = []                      # no positive regimes, no closed history
    def __iter__(self):
        return iter(self._rows)
    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, sleeve_ids):
        self._cur = _Cur(sleeve_ids)
    def cursor(self):
        return self._cur
    def close(self):
        pass


def _manifest(tmp_path, monkeypatch):
    root = tmp_path
    (root / 'src' / 'strategies').mkdir(parents=True)
    (root / 'src' / 'strategies' / 'manifest.json').write_text(json.dumps({'strategies': {
        'S_beta_spy': {'state': 'live', 'history': []},
        'S_alpha': {'state': 'live', 'history': []},
    }}))
    monkeypatch.setattr(sw, 'ROOT', root)
    monkeypatch.setattr(sw, '_strategies_in_grace_period', lambda manifest, grace_days: set())


def test_sleeve_excluded_alpha_still_demotable(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    out = sw.find_negative_across_all_eligible(conn=_Conn({'S_beta_spy'}), grace_days=0)
    assert out == ['S_alpha']


def test_no_sleeves_is_the_old_behaviour(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    out = sw.find_negative_across_all_eligible(conn=_Conn(set()), grace_days=0)
    assert sorted(out) == ['S_alpha', 'S_beta_spy']
