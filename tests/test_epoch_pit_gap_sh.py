"""Tests for scripts/epoch_pit_gap.sh (tmp --root/--etc, NO_SYSTEMCTL=1)."""
import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "epoch_pit_gap.sh"
FLIP = Path(__file__).resolve().parents[1] / "scripts" / "pit_gap_flip_after_fleet.sh"
DATE = "20261024"
DEADLINE = "2026-10-25T20:00"


def run(root, etc, *args, env_extra=None):
    env = dict(os.environ, NO_SYSTEMCTL="1")
    env.update(env_extra or {})
    return subprocess.run(["bash", str(SCRIPT), "--root", str(root), "--etc", str(etc),
                           "--date", DATE, *args],
                          capture_output=True, text=True, env=env, timeout=60)


def mk(tmp_path):
    root, etc = tmp_path / "root", tmp_path / "etc"
    (root / "data").mkdir(parents=True)
    etc.mkdir()
    return root, etc


def ledgers(root, done="a\nb\n", failed="x\n"):
    (root / "data" / ".refresh_backtests.done").write_text(done)
    (root / "data" / ".refresh_backtests.done.failed").write_text(failed)


def paths(root, etc):
    d = "openclaw-fleet-overnight-resume.service.d/pit-gap.conf"
    return (root / "docs/systemd" / d, etc / d,
            etc / f"fleet-pit-gap-epoch-{DATE}.service", etc / f"fleet-pit-gap-epoch-{DATE}.timer")


def test_syntax_and_executable():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_dropin_path_matches_flip_script():
    m = re.search(r"^DROPIN=(\S+)", FLIP.read_text(), re.M)
    assert m
    root, etc = Path("/R"), Path("/etc/systemd/system")
    assert m.group(1) == str(paths(root, etc)[1])


def test_dry_run_install_prints_plan_without_mutating(tmp_path):
    root, etc = mk(tmp_path)
    r = run(root, etc, "install", "--deadline", DEADLINE)
    assert r.returncode == 0, r.stderr
    assert "[dry-run] install" in r.stdout and "[plan]" in r.stdout
    assert not any(p.exists() for p in paths(root, etc))


def test_install_writes_files_and_refuses_overwrite(tmp_path):
    root, etc = mk(tmp_path)
    r = run(root, etc, "install", "--run", "--deadline", DEADLINE)
    assert r.returncode == 0, r.stderr
    snap, live, unit, timer = paths(root, etc)
    assert snap.exists() and live.exists() and unit.exists() and not timer.exists()
    assert snap.read_text() == live.read_text()
    txt = live.read_text()
    assert txt.startswith("[Service]\n") and "#" in txt
    for flag in ('"OPENCLAW_FINANCIALS_PIT=1"', '"OPENCLAW_BT_GAP_FILL=open"', '"OPENCLAW_MACRO_PIT=1"'):
        assert flag in txt
    u = unit.read_text()
    assert 'ExecStart="/bin/bash" "%s/scripts/fleet_weekend_window.sh" "--deadline" "%s"' % (root, DEADLINE) in u
    for flag in ('"OPENCLAW_FINANCIALS_PIT=1"', '"OPENCLAW_BT_GAP_FILL=open"', '"OPENCLAW_MACRO_PIT=1"'):
        assert flag in u
    r2 = run(root, etc, "install", "--run", "--deadline", DEADLINE)
    assert r2.returncode == 1 and "REFUSING to overwrite" in r2.stderr


def test_install_with_timer(tmp_path):
    root, etc = mk(tmp_path)
    r = run(root, etc, "install", "--run", "--deadline", DEADLINE, "--on-calendar", "Sat 08:05 UTC")
    assert r.returncode == 0, r.stderr
    timer = paths(root, etc)[3]
    t = timer.read_text()
    assert "OnCalendar=Sat 08:05 UTC" in t and f"Unit=fleet-pit-gap-epoch-{DATE}.service" in t


def test_install_requires_deadline(tmp_path):
    root, etc = mk(tmp_path)
    assert run(root, etc, "install", "--run").returncode == 2


def test_env_file_warning(tmp_path):
    root, etc = mk(tmp_path)
    (root / ".env").write_text("FOO=1\nOPENCLAW_MACRO_PIT=1\n")
    r = run(root, etc, "install", "--deadline", DEADLINE)
    assert "WARNING" in r.stderr and "OPENCLAW_MACRO_PIT" in r.stderr
    assert "OPENCLAW_FINANCIALS_PIT" not in r.stderr
    (root / ".env").write_text("FOO=1\n")
    assert "WARNING" not in run(root, etc, "install", "--deadline", DEADLINE).stderr


def test_checkpoint_and_refuse_overwrite(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    assert run(root, etc, "checkpoint").returncode == 0
    assert (root / f"data/.refresh_backtests.done.pre-pit-gap-{DATE}").read_text() == "a\nb\n"
    assert run(root, etc, "checkpoint").returncode == 1


def test_rotate_refuses_before_install(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "checkpoint")
    r = run(root, etc, "rotate", "--run")
    assert r.returncode == 1 and "not installed" in r.stderr
    assert (root / "data/.refresh_backtests.done").exists()


def test_rotate_refuses_when_checkpoint_differs(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "checkpoint")
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    (root / "data/.refresh_backtests.done").write_text("a\nb\nc\n")
    r = run(root, etc, "rotate", "--run")
    assert r.returncode == 1 and "differs from its checkpoint" in r.stderr


def test_rotate_requires_checkpoint(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    r = run(root, etc, "rotate", "--run")
    assert r.returncode == 1 and "checkpoint" in r.stderr


def test_rotate_dry_run_does_not_move(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "checkpoint")
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    r = run(root, etc, "rotate")
    assert r.returncode == 0 and "[dry-run] rotate" in r.stdout
    assert (root / "data/.refresh_backtests.done").exists()


def test_rotate_moves_ledgers_and_stamps_since(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "checkpoint")
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    r = run(root, etc, "rotate", "--run")
    assert r.returncode == 0, r.stderr
    d = root / "data"
    assert not (d / ".refresh_backtests.done").exists()
    assert not (d / ".refresh_backtests.done.failed").exists()
    assert (d / f".refresh_backtests.done.rotated-pre-pit-gap-{DATE}").read_text() == "a\nb\n"
    since = d / f".refresh_backtests.done.pre-pit-gap-{DATE}.since"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d\n", since.read_text())
    assert (d / f".refresh_backtests.done.pre-pit-gap-{DATE}").exists()


def test_status_dry_run(tmp_path):
    root, etc = mk(tmp_path)
    r = run(root, etc, "status", "--dry-run")
    assert r.returncode == 0
    assert "pit_gap_flip_gate.py --max-lagging 3" in r.stdout


def test_uninstall_removes_and_restores(tmp_path):
    root, etc = mk(tmp_path)
    ledgers(root)
    run(root, etc, "checkpoint")
    run(root, etc, "install", "--run", "--deadline", DEADLINE, "--on-calendar", "Sat 08:05 UTC")
    run(root, etc, "rotate", "--run")
    r = run(root, etc, "uninstall", "--run", "--restore-ledgers")
    assert r.returncode == 0, r.stderr
    assert not any(p.exists() for p in paths(root, etc))
    assert (root / "data/.refresh_backtests.done").read_text() == "a\nb\n"
    assert (root / "data/.refresh_backtests.done.failed").read_text() == "x\n"


def test_uninstall_dry_run_keeps_files(tmp_path):
    root, etc = mk(tmp_path)
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    r = run(root, etc, "uninstall")
    assert r.returncode == 0 and "[dry-run] uninstall" in r.stdout
    assert paths(root, etc)[1].exists()


def test_uninstall_resolves_date_without_flag(tmp_path):
    root, etc = mk(tmp_path)
    run(root, etc, "install", "--run", "--deadline", DEADLINE)
    env = dict(os.environ, NO_SYSTEMCTL="1")
    r = subprocess.run(["bash", str(SCRIPT), "--root", str(root), "--etc", str(etc),
                        "uninstall", "--run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and f"resolved to {DATE}" in r.stdout
    assert not paths(root, etc)[2].exists()
