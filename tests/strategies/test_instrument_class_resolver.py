import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import json
from strategies.instrument_class import instrument_class_for, VALID_INSTRUMENT_CLASSES  # noqa


def _manifest(tmp_path, mapping):
    p = tmp_path / 'manifest.json'
    strategies = {sid: {'state': 'live', 'state_since': '2026-05-01T00:00:00Z',
                        'metadata': {}, 'history': [], 'instrument_class': ic}
                  for sid, ic in mapping.items()}
    p.write_text(json.dumps({'strategies': strategies}))
    return p


def test_resolves_declared_class(tmp_path):
    p = _manifest(tmp_path, {'s1': 'etp', 's2': 'equity'})
    assert instrument_class_for('s1', manifest_path=p) == 'etp'


def test_unknown_strategy_defaults_equity(tmp_path):
    p = _manifest(tmp_path, {'s1': 'etp'})
    assert instrument_class_for('nope', manifest_path=p) == 'equity'
