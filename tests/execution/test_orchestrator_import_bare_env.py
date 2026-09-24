"""Regression: the orchestrator (and everything that imports it) must import
under a BARE environment — no PYTHONPATH — because the pre-market scan unit
(`openclaw-premarket-scan@.service`) runs `python3 src/pipeline/run_premarket_scan.py`
with only `EnvironmentFile=/root/openclaw/.env`, which sets no PYTHONPATH.

2026-09-16..22: wave 1 added `from lib import run_lock` to
`src/execution/pipeline_orchestrator.py`; `lib` lives under `<ROOT>/src`, which
the module put on `sys.path` only via pytest/johnbot, so both pre-market panic
scans failed with ModuleNotFoundError on every trading day for a week.

Run: python3 -m pytest tests/execution/test_orchestrator_import_bare_env.py -q
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bare_import(module: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
    env['OPENCLAW_STEP_MEMORY_MAX'] = '0'
    return subprocess.run(
        [sys.executable, '-c', f'import {module}'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def test_orchestrator_imports_without_pythonpath():
    r = _bare_import('src.execution.pipeline_orchestrator')
    assert r.returncode == 0, r.stderr[-800:]


def test_premarket_helpers_import_without_pythonpath():
    r = _bare_import('src.pipeline.premarket_helpers')
    assert r.returncode == 0, r.stderr[-800:]


# QD E3 fix round 1 (this branch only — proc_heartbeat doesn't exist on
# main): `run_premarket_scan.main()` does `from lib import proc_heartbeat`,
# deferred inside the function body rather than at module import time, so
# merely importing `src.pipeline.run_premarket_scan` (as the two tests above
# import their modules) does NOT exercise that statement — it only proves
# the module *parses* under a bare environment. This test explicitly imports
# both `src.pipeline.run_premarket_scan` (which, via premarket_helpers,
# transitively imports pipeline_orchestrator and so runs the sys.path fix
# above) AND `lib.proc_heartbeat` in the same bare-environment subprocess,
# proving the fix actually reaches run_premarket_scan's own heartbeat call
# site, not just the module that carries the fix.
def test_premarket_scan_and_proc_heartbeat_import_without_pythonpath():
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
    env['OPENCLAW_STEP_MEMORY_MAX'] = '0'
    r = subprocess.run(
        [sys.executable, '-c',
         'import src.pipeline.run_premarket_scan; import lib.proc_heartbeat'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]


# ── F5 / review M-1: script-mode bare-env probes ────────────────────────────
# The tests above import these modules by DOTTED PATH (`python3 -c "import
# module"`), which proves the module parses under a bare environment but
# never exercises how these two scripts are actually invoked in production:
# as a FILE (`python3 <path>`), with PYTHONPATH unset, a cwd outside the
# repo, and POSTGRES_URI empty. Script mode matters here specifically
# because both files put themselves on sys.path from their own __file__ at
# import time (no reliance on a caller-set PYTHONPATH), so the dotted-import
# probe above can pass for reasons that don't hold for the script-mode path.

def _script_mode_env():
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
    env['OPENCLAW_STEP_MEMORY_MAX'] = '0'
    env['POSTGRES_URI'] = ''
    return env


def test_account_breaker_script_mode_imports_without_running_main(tmp_path):
    """account_breaker.py's `if __name__ == '__main__': sys.exit(main())`
    calls main() UNCONDITIONALLY — main() takes no argv and has no --help,
    so `python3 account_breaker.py` as a plain subprocess would run a real
    breaker tick (DB connect attempt) rather than just proving the import is
    safe. Load it via runpy with run_name != '__main__' instead: every
    top-level def/class/constant still executes (the actual bare-env import
    surface), but the __main__ guard's body never fires, so no DB/HTTP call
    happens. PYTHONPATH unset, cwd outside the repo (tmp_path),
    POSTGRES_URI empty."""
    script = str(ROOT / 'src' / 'execution' / 'account_breaker.py')
    # Re-review finding 2: the script's top-level imports are stdlib-only
    # (its execution.* imports are lazy), so a bare runpy load passes even
    # without the script's own sys.path.insert. Pin the OUTAGE CLASS instead:
    # after the load, `lib` and `execution` must resolve under ROOT/src
    # (find_spec does not import them — no DB/HTTP call).
    code = ('import runpy, importlib.util, os\n'
           f'runpy.run_path({script!r}, run_name="not_main")\n'
           f'src = os.path.realpath({str(ROOT / "src")!r})\n'
            'for name in ("lib", "execution"):\n'
            '    spec = importlib.util.find_spec(name)\n'
            '    assert spec is not None, name + " not importable"\n'
            '    locs = [os.path.realpath(p) for p in (spec.submodule_search_locations or [])]\n'
            '    assert any(l.startswith(src) for l in locs), (name, locs)\n'
            'print("IMPORT_OK")\n')
    r = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(tmp_path), env=_script_mode_env(), capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert 'IMPORT_OK' in r.stdout


def test_ingest_macro_events_script_mode_help_bare_env(tmp_path):
    """ingest_macro_events.py DOES support --help, so running it as a
    script (`python3 <path> --help`, the literal production invocation
    shape) is the most direct script-mode probe: argparse exits via
    SystemExit(0) before main() reaches any fetch/DB/parquet-write code.
    PYTHONPATH unset, cwd outside the repo (tmp_path), POSTGRES_URI empty."""
    script = str(ROOT / 'src' / 'ingestion' / 'ingest_macro_events.py')
    r = subprocess.run(
        [sys.executable, script, '--help'],
        cwd=str(tmp_path), env=_script_mode_env(), capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert 'usage' in r.stdout.lower()


def test_ingest_macro_events_script_mode_puts_lib_and_src_on_path(tmp_path):
    """Re-review finding 2: `--help` exits inside argparse before anything
    sys.path-dependent runs, so it cannot pin the 09-16..22 outage class.
    Load the script via runpy with run_name != '__main__' (no fetch/DB/
    parquet code runs) and assert that `lib` resolves under ROOT/src and
    `src` under ROOT afterwards — the two import roots its lazy imports need."""
    script = str(ROOT / 'src' / 'ingestion' / 'ingest_macro_events.py')
    code = ('import runpy, importlib.util, os\n'
           f'runpy.run_path({script!r}, run_name="not_main")\n'
           f'root = os.path.realpath({str(ROOT)!r}); src = os.path.join(root, "src")\n'
            'for name, base in (("lib", src), ("src", root)):\n'
            '    spec = importlib.util.find_spec(name)\n'
            '    assert spec is not None, name + " not importable"\n'
            '    locs = [os.path.realpath(p) for p in (spec.submodule_search_locations or [])]\n'
            '    assert any(l == base or l.startswith(base + os.sep) for l in locs), (name, locs)\n'
            'print("IMPORT_OK")\n')
    r = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(tmp_path), env=_script_mode_env(), capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert 'IMPORT_OK' in r.stdout
