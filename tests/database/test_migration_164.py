"""Static parse of migration 164 / 093 (never connects to Postgres)."""
import re
from pathlib import Path

MIG = Path(__file__).resolve().parents[2] / "src" / "database" / "migrations"
F164 = MIG / "164_drop_dead_backtest_trades_indexes.sql"


def _sql_lines(path):
    return [l for l in path.read_text().splitlines()
            if l.strip() and not l.strip().startswith("--")]


def test_164_exists_and_idempotent():
    assert F164.exists()
    text = F164.read_text()
    assert "DROP INDEX IF EXISTS idx_backtest_trades_strategy;" in text
    assert "DROP INDEX IF EXISTS idx_backtest_trades_regime;" in text
    assert not any("CONCURRENTLY" in l for l in _sql_lines(F164))


def test_164_bounded_lock_wait():
    lines = [l.strip() for l in _sql_lines(F164)]
    assert lines[0] == "BEGIN;" and lines[-1] == "COMMIT;"
    idx = [i for i, l in enumerate(lines) if l.startswith("SET LOCAL lock_timeout")]
    assert idx and 0 < idx[0] < len(lines) - 1


def test_093_no_longer_creates_dropped_indexes():
    pat = re.compile(r"CREATE INDEX.*idx_backtest_trades_(strategy|regime)", re.I)
    for f in sorted(MIG.glob("*.sql")):
        txt = "\n".join(_sql_lines(f))
        # statements may span lines; collapse whitespace
        assert not pat.search(re.sub(r"\s+", " ", txt)), f.name


def test_164_is_next_number():
    names = sorted(p.name for p in MIG.glob("*.sql"))
    assert [n for n in names if n.startswith("164_")] == [F164.name]
    after = names[names.index(F164.name):]
    assert not [n for n in names[:names.index(F164.name)] if re.match(r"16[5-9]_", n)]
    assert after[0] == F164.name
