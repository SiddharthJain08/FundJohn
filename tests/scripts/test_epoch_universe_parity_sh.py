"""scripts/epoch_universe_parity.sh — dry-run output per subcommand, in a temp --root/--etc.
Never touches /root/openclaw/data or /etc. Run:
  PYTHONPATH=src python3 -m pytest tests/scripts/test_epoch_universe_parity_sh.py -q"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / 'scripts' / 'epoch_universe_parity.sh'
D = '20261010'


def sh(tmp, *args, env=None):
    import os
    e = dict(os.environ, NO_SYSTEMCTL='1', PYTHON='echo', **(env or {}))
    return subprocess.run(['bash', str(SH), *args, '--root', str(tmp / 'root'), '--etc', str(tmp / 'etc'), '--date', D],
                          capture_output=True, text=True, env=e)


@pytest.fixture
def tmp(tmp_path):
    (tmp_path / 'root' / 'data').mkdir(parents=True)
    (tmp_path / 'root' / 'data' / '.refresh_backtests.done').write_text('S_a\nS_b\n')
    (tmp_path / 'root' / 'data' / '.refresh_backtests.done.failed').write_text('S_c\trc=1\tx\n')
    return tmp_path


def test_never_sources_env():
    src = SH.read_text()
    code = [l for l in src.splitlines() if not l.lstrip().startswith('#')]
    for l in code:
        assert not l.lstrip().startswith(('. ', 'source ')), l
        assert 'cat ' not in l or '.env' not in l, l
    assert 'EnvironmentFile=' in src


def test_checkpoint_copies_and_refuses_overwrite(tmp):
    r = sh(tmp, 'checkpoint')
    assert r.returncode == 0, r.stderr
    d = tmp / 'root' / 'data'
    assert (d / f'.refresh_backtests.done.pre-universe-parity-{D}').read_text() == 'S_a\nS_b\n'
    assert (d / f'.refresh_backtests.done.failed.pre-universe-parity-{D}').exists()
    assert (d / '.refresh_backtests.done').exists()          # live ledger untouched
    r2 = sh(tmp, 'checkpoint')
    assert r2.returncode == 1 and 'REFUSING' in r2.stderr


def test_checkpoint_dry_run_writes_nothing(tmp):
    r = sh(tmp, 'checkpoint', '--dry-run')
    assert r.returncode == 0 and f'pre-universe-parity-{D}' in r.stdout
    assert not list((tmp / 'root' / 'data').glob('*pre-universe*'))


def test_rotate_requires_checkpoint_and_run(tmp):
    assert sh(tmp, 'rotate', '--run').returncode == 1                   # no checkpoint yet
    sh(tmp, 'checkpoint')
    r = sh(tmp, 'rotate')
    assert 'dry-run' in r.stdout and (tmp / 'root' / 'data' / '.refresh_backtests.done').exists()
    r = sh(tmp, 'rotate', '--run')
    assert r.returncode == 0, r.stderr
    d = tmp / 'root' / 'data'
    assert not (d / '.refresh_backtests.done').exists()
    assert (d / f'.refresh_backtests.done.rotated-pre-universe-parity-{D}').exists()
    assert (d / f'.refresh_backtests.done.pre-universe-parity-{D}').exists()


def test_artifact_dry_run_prints_systemd_run(tmp):
    r = sh(tmp, 'artifact')
    assert r.returncode == 0 and 'dry-run' in r.stdout
    out = r.stdout
    for frag in ('systemd-run --wait --pipe --collect', f'--property=EnvironmentFile={tmp}/root/.env',
                 '--property=Nice=19', '--property=MemoryMax=3500M', 'scripts/build_tier_membership.py',
                 f'--run-id shrink-{D}', '--start 2016-03-01', '--end 2026-10-10', '--out-dir data',
                 f'universe_tier_membership_shrink-{D}.parquet'):
        assert frag in out, frag
    assert not list((tmp / 'root' / 'data').glob('universe_tier*'))


def test_artifact_refuses_existing(tmp):
    (tmp / 'root' / 'data' / f'universe_tier_membership_shrink-{D}.parquet').write_text('x')
    r = sh(tmp, 'artifact', '--run')
    assert r.returncode == 1 and 'REFUSING' in r.stderr


def test_install_dry_run_prints_units_and_writes_nothing(tmp):
    r = sh(tmp, 'install', '--deadline', '2026-10-12T10:30', '--on-calendar', '2026-10-10 08:05:00 UTC')
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert '[Service]\n' in out and 'Environment="OPENCLAW_BT_UNIVERSE_FILTER_REF=1"' in out
    assert f'fleet-universe-parity-epoch-{D}.service' in out
    assert '"--deadline" "2026-10-12T10:30"' in out and 'fleet_weekend_window.sh' in out
    assert 'OnCalendar=2026-10-10 08:05:00 UTC' in out
    assert not (tmp / 'etc').exists() and not (tmp / 'root' / 'docs').exists()


def test_install_requires_deadline(tmp):
    assert sh(tmp, 'install').returncode == 2
    assert sh(tmp, 'install', '--deadline', 'tomorrow').returncode == 2


def test_install_run_then_uninstall_run(tmp):
    r = sh(tmp, 'install', '--run', '--deadline', '2026-10-12T10:30')
    assert r.returncode == 0, r.stderr
    dp = tmp / 'etc' / 'openclaw-fleet-overnight-resume.service.d' / 'universe-parity.conf'
    assert dp.read_text().count('Environment="OPENCLAW_BT_UNIVERSE_FILTER_REF=1"') == 1
    unit = (tmp / 'etc' / f'fleet-universe-parity-epoch-{D}.service').read_text()
    assert 'OPENCLAW_BT_UNIVERSE_FILTER_REF=1' in unit and 'RuntimeMaxSec=99000' in unit
    assert 'systemctl start --no-block' in r.stdout
    assert sh(tmp, 'install', '--run', '--deadline', '2026-10-12T10:30').returncode == 1   # no overwrite
    r = sh(tmp, 'uninstall')
    assert 'dry-run' in r.stdout and dp.exists()
    r = sh(tmp, 'uninstall', '--run')
    assert r.returncode == 0 and not dp.exists()
    assert not (tmp / 'etc' / f'fleet-universe-parity-epoch-{D}.service').exists()


def test_install_warns_when_env_defines_flag(tmp):
    (tmp / 'root' / '.env').write_text('OPENCLAW_BT_UNIVERSE_FILTER_REF=0\nSECRET=hunter2\n')
    r = sh(tmp, 'install', '--deadline', '2026-10-12T10:30')
    assert 'WARNING' in r.stderr and 'hunter2' not in r.stdout + r.stderr


def test_status_and_report_commands(tmp):
    r = sh(tmp, 'status', '--dry-run')
    assert 'universe_parity_gate.py' in r.stdout and '--env-file' in r.stdout
    r = sh(tmp, 'report', '--dry-run')
    assert 'universe_parity_report.py' in r.stdout and f'universe-parity-report-{D}' in r.stdout
    r = sh(tmp, 'status')            # PYTHON=echo: runs the gate command line
    assert r.returncode == 0 and 'universe_parity_gate.py' in r.stdout


def test_unknown_subcommand(tmp):
    assert sh(tmp, 'bogus').returncode == 2
