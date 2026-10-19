"""Migration 165 — static DDL shape (no DB connection)."""
import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / 'src' / 'database' / 'migrations'
       / '165_mc_runs_seed.sql').read_text()


def test_adds_seed_to_both_tables():
    for t in ('strategy_regime_intraday_mc_runs', 'strategy_regime_mc_runs'):
        m = re.search(rf'ALTER TABLE {t}\s+ADD COLUMN IF NOT EXISTS seed BIGINT', SQL)
        assert m, t


def test_additive_only():
    code = re.sub(r'--[^\n]*', '', SQL)
    assert re.search(r'\b(DELETE|DROP|TRUNCATE|UPDATE|RENAME)\b', code, re.I) is None
