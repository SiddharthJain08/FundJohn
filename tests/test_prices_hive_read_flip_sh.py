"""Tests for scripts/prices_hive_read_flip.sh (temp env file / parity log, fake systemctl)."""
import glob
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prices_hive_read_flip.sh"
PASS = ("[prices_hive_parity] rows_file=10 rows_hive=10 dup_keys_hive=0 ticker_maxdate_mismatch=0 "
        "sample_n=5 sample_mismatch=0 verdict=PASS")
FAIL = PASS.replace("rows_hive=10", "rows_hive=9").replace("verdict=PASS", "verdict=FAIL")


def parity_log(path, runs):
    """runs: list of (date, 'PASS'|'FAIL')"""
    out = []
    for d, v in runs:
        out.append(f"[{d}T21:05:01Z] prices_hive_parity run")
        out.append(PASS if v == "PASS" else FAIL)
    path.write_text("\n".join(out) + "\n")


def dates(n):
    return [f"2026-10-{20 + i:02d}" for i in range(n)]


def setup(tmp_path, env="OPENCLAW_PRICES_HIVE=1\nOTHER=x\n", runs=None):
    envf, plog = tmp_path / "env", tmp_path / "parity.log"
    envf.write_text(env)
    parity_log(plog, runs if runs is not None else [(d, "PASS") for d in dates(5)])
    shim = tmp_path / "shim"
    shim.mkdir()
    calls = tmp_path / "systemctl.calls"
    (shim / "systemctl").write_text(f'#!/bin/bash\necho "$@" >> {calls}\nexit 0\n')
    (shim / "systemctl").chmod(0o755)
    return envf, plog, shim, calls


def run(tmp_path, envf, plog, shim, *extra, now="6 1000"):
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", PRICES_HIVE_FLIP_NOW=now)
    return subprocess.run(
        ["bash", str(SCRIPT), "--env-file", str(envf), "--parity-log", str(plog),
         "--log", str(tmp_path / "flip.log"), "--no-restart", "--no-post",
         "--timer-unit", "dummy.timer", *extra],
        capture_output=True, text=True, env=env, timeout=60)


def test_executable_and_syntax():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_already_applied(tmp_path):
    envf, plog, shim, calls = setup(tmp_path, env="OPENCLAW_PRICES_HIVE=1\nOPENCLAW_PRICES_HIVE_READ=1\n")
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert r.returncode == 0 and "already applied" in r.stdout
    assert "disable --now dummy.timer" in calls.read_text()
    assert glob.glob(str(envf) + ".bak.*") == []


def test_write_flag_missing_not_yet(tmp_path):
    envf, plog, shim, calls = setup(tmp_path, env="OTHER=x\n")
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert r.returncode == 0 and "NOT_YET" in r.stdout and "OPENCLAW_PRICES_HIVE=1" in r.stdout
    assert "READ" not in envf.read_text()


def test_hive_read_flag_alone_does_not_satisfy_g1(tmp_path):
    envf, plog, shim, _ = setup(tmp_path, env="OPENCLAW_PRICES_HIVE_X=1\n")
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert "NOT_YET" in r.stdout


def test_fewer_than_five_distinct_dates(tmp_path):
    # 6 PASS lines but only 4 distinct dates
    runs = [(d, "PASS") for d in dates(4)] + [(dates(4)[-1], "PASS"), (dates(4)[0], "PASS")]
    envf, plog, shim, _ = setup(tmp_path, runs=runs)
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert r.returncode == 0 and "verdict: NOT_YET" in r.stdout and "distinct-date PASS" in r.stdout
    assert "READ" not in envf.read_text()


def test_five_pass_but_later_fail(tmp_path):
    d = dates(7)
    runs = [(x, "PASS") for x in d[:5]] + [(d[5], "FAIL"), (d[6], "PASS")]
    envf, plog, shim, _ = setup(tmp_path, runs=runs)
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert "verdict: NOT_YET" in r.stdout and "FAIL after" in r.stdout


def test_latest_fail_blocks(tmp_path):
    d = dates(6)
    runs = [(x, "PASS") for x in d[:5]] + [(d[5], "FAIL")]
    envf, plog, shim, _ = setup(tmp_path, runs=runs)
    assert "verdict: NOT_YET" in run(tmp_path, envf, plog, shim).stdout


def test_old_fail_before_window_is_ignored(tmp_path):
    d = dates(7)
    runs = [(d[0], "FAIL")] + [(x, "PASS") for x in d[1:6]]
    envf, plog, shim, _ = setup(tmp_path, runs=runs)
    assert "verdict: OK" in run(tmp_path, envf, plog, shim).stdout


def test_missing_parity_log(tmp_path):
    envf, plog, shim, _ = setup(tmp_path)
    plog.unlink()
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert r.returncode == 0 and "NOT_YET" in r.stdout


def test_check_only_met_does_not_change_file(tmp_path):
    envf, plog, shim, calls = setup(tmp_path)
    before = envf.read_text()
    r = run(tmp_path, envf, plog, shim)
    assert r.returncode == 0 and "gate MET" in r.stdout
    assert envf.read_text() == before
    assert glob.glob(str(envf) + ".bak.*") == []
    assert not calls.exists()


def test_apply_sets_flag_backup_and_disables_timer(tmp_path):
    envf, plog, shim, calls = setup(tmp_path)
    before = envf.read_text()
    r = run(tmp_path, envf, plog, shim, "--apply")
    assert r.returncode == 0, r.stderr
    assert "OPENCLAW_PRICES_HIVE_READ=1" in envf.read_text().splitlines()
    baks = glob.glob(str(envf) + ".bak.hive-read-flip.*")
    assert len(baks) == 1 and Path(baks[0]).read_text() == before
    assert "disable --now dummy.timer" in calls.read_text()
    assert "--user" not in calls.read_text()  # --no-restart


def test_apply_replaces_existing_zero_value(tmp_path):
    envf, plog, shim, _ = setup(tmp_path, env="OPENCLAW_PRICES_HIVE=1\nOPENCLAW_PRICES_HIVE_READ=0\n")
    run(tmp_path, envf, plog, shim, "--apply")
    lines = envf.read_text().splitlines()
    assert "OPENCLAW_PRICES_HIVE_READ=1" in lines and "OPENCLAW_PRICES_HIVE_READ=0" not in lines


def test_apply_refused_inside_compute_window(tmp_path):
    envf, plog, shim, _ = setup(tmp_path)
    r = run(tmp_path, envf, plog, shim, "--apply", now="3 1400")
    assert r.returncode == 0 and "compute window" in r.stdout
    assert "READ" not in envf.read_text()
