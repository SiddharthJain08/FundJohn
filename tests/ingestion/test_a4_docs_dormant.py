"""A4: close-proxy docstring matches code default; dormant Polygon 30m ingester deleted."""
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_close_proxy_doc_default_matches_code():
    import ingestion.close_proxy_snapshot as mod

    src = inspect.getsource(mod)
    m = re.search(r'OPENCLAW_CLOSE_PROXY_MIN_COVERAGE", "([0-9.]+)"', src)
    assert m, "code default not found"
    assert f"({m.group(1)})" in mod.fetch_close_proxy.__doc__


def test_polygon_30m_ingester_deleted():
    assert not (ROOT / "src/ingestion/ingest_prices_30m.py").exists()
    offenders = []
    for base in ("src", "scripts", "docs/systemd"):
        for p in (ROOT / base).rglob("*"):
            if p.suffix not in {".py", ".js", ".service", ".timer"} or not p.is_file():
                continue
            if p == Path(__file__):
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if "ingest_prices_30m.py" in line and not any(w in line for w in ("retired", "deleted", "purged")):
                    offenders.append(f"{p.relative_to(ROOT)}:{i}")
    assert not offenders, offenders
