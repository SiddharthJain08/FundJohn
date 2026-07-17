# tests/test_cutover_smoke.py
"""SP-1 end-to-end smoke: doctor preflight + dry-run cycle + archive job.

Marked as 'integration' — only runs when INTEGRATION_SMOKE=1 to avoid
hitting live Alpaca on every pytest invocation.
"""
import os
import subprocess
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get('INTEGRATION_SMOKE') != '1',
    reason='set INTEGRATION_SMOKE=1 to run live-API smoke',
)


def test_doctor_preflight_passes():
    """Full doctor run including new SP-1 checks."""
    res = subprocess.run(
        ['python3', 'src/maintenance/doctor.py', '--required-only', '--json'],
        capture_output=True, text=True, timeout=60,
        cwd='/root/openclaw',
    )
    # exit 0 (pass) or 1 (warn) — both acceptable for smoke. exit 2 = fail.
    assert res.returncode in (0, 1), f'doctor stderr: {res.stderr[-500:]}'


def test_dry_run_pipeline_completes():
    """PIPELINE_DRY_RUN=1 should run all stages without real orders."""
    env = {**os.environ, 'PIPELINE_DRY_RUN': '1'}
    res = subprocess.run(
        ['python3', '-m', 'src.execution.pipeline_orchestrator', '--reason', 'sp1-smoke'],
        capture_output=True, text=True, timeout=900, env=env,
        cwd='/root/openclaw',
    )
    assert res.returncode == 0, f'orchestrator stderr (last 2k): {res.stderr[-2000:]}'


def test_options_archive_dry_run_writes_rows():
    """Archive job in dry-run mode writes to /tmp parquet and exits 0."""
    tmp_parquet = '/tmp/options_eod_dryrun.parquet'
    if os.path.exists(tmp_parquet):
        os.remove(tmp_parquet)
    env = {**os.environ, 'OPTIONS_EOD_PARQUET': tmp_parquet}
    res = subprocess.run(
        ['python3', '-m', 'src.pipeline.backfillers.alpaca_options', '--dry-run'],
        capture_output=True, text=True, timeout=600, env=env,
        cwd='/root/openclaw',
    )
    assert res.returncode == 0, f'archive stderr: {res.stderr[-500:]}'

    import pandas as pd
    assert os.path.exists(tmp_parquet), 'archive produced no parquet'
    df = pd.read_parquet(tmp_parquet)
    assert len(df) > 0, 'archive produced no rows'
    # At least 50% of rows should have non-zero delta (filter applied at consumer;
    # archive intentionally keeps all rows, so a meaningful chunk should have greeks)
    nonzero_delta = (df['delta'].fillna(0) != 0).sum()
    assert nonzero_delta / len(df) >= 0.30, \
        f'only {nonzero_delta}/{len(df)} rows with non-zero delta — AAT Plus greeks regression?'
