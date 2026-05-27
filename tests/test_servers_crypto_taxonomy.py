"""SP-4: servers.json declares the crypto data availability taxonomy without
mutating any covered_columns (so the capability gate stays correct).
Run: pytest tests/test_servers_crypto_taxonomy.py -v
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / 'src' / 'agent' / 'config' / 'servers.json'


def test_taxonomy_present_and_shaped():
    data = json.loads(SERVERS.read_text())
    tax = data['crypto_data_taxonomy']
    assert 'prices' in ' '.join(tax['available']).lower()
    for col in ('funding_rate', 'perp_oi', 'order_book'):
        assert col in tax['unavailable']


def test_unavailable_cols_not_in_any_covered_columns():
    data = json.loads(SERVERS.read_text())
    covered = set()
    for s in data['servers']:
        covered.update(s.get('covered_columns', []))
    for col in ('funding_rate', 'perp_oi', 'order_book', 'spot_vol'):
        assert col not in covered


def test_servers_array_intact():
    data = json.loads(SERVERS.read_text())
    names = {s['name'] for s in data['servers']}
    assert {'fmp', 'alpaca', 'sec_edgar', 'tavily'} <= names
