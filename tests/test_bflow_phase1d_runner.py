"""End-to-end on a 3-session synthetic temp cache. The runner must (a) emit
policy_rows.parquet + report.md with a [bflow-p1d] VERDICT line, (b) be
cache-only (reads parquets; no network), (c) honor --limit."""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest


def _session_df(price=100.0, dip=None):
    rows = []
    for m in range(390):
        p = price * 0.95 if (dip and dip[0] <= m <= dip[1]) else price
        rows.append({"ticker": "AAA", "minute": m, "o": p, "h": p + 0.2,
                     "l": p - 0.2, "c": p, "v": 1000.0, "vw": p})
    return pd.DataFrame(rows)


@pytest.fixture
def tmp_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for d, dip in [("2024-01-02", (60, 80)), ("2024-01-03", None),
                   ("2024-01-04", (200, 220))]:
        _session_df(dip=dip).to_parquet(cache / f"min_bars_{d}.parquet")
    return str(cache)


def test_runner_end_to_end(tmp_cache, tmp_path):
    analysis = str(tmp_path / "analysis")
    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1d.py",
         "--cache-dir", tmp_cache, "--analysis-dir", analysis],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "[bflow-p1d] VERDICT" in proc.stdout
    assert "## Diagnostics" in proc.stdout
    assert "excluded_thin_null" in proc.stdout
    assert os.path.exists(os.path.join(analysis, "report.md"))
    rows = pd.read_parquet(os.path.join(analysis, "policy_rows.parquet"))
    # 3 sessions x 1 ticker x 6 cells = 18 audit rows (pre-scoring; with only
    # 3 sessions every triggered row is thin-null-excluded from SCORING but
    # still present in the parquet)
    assert len(rows) == 18
    assert rows["entry_minute"].dtype == np.float64


def test_runner_limit(tmp_cache, tmp_path):
    analysis = str(tmp_path / "analysis2")
    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1d.py",
         "--cache-dir", tmp_cache, "--analysis-dir", analysis, "--limit", "1"],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")
    assert proc.returncode == 0, proc.stderr[-2000:]
    rows = pd.read_parquet(os.path.join(analysis, "policy_rows.parquet"))
    assert len(rows) == 6                 # 1 session x 1 ticker x 6 cells
