"""tests/maintenance/test_doctor_co_tenant_memory.py — `check_co_tenant_memory`
ruled semantics (QD E3 fix round 1, ruled item 4): ALWAYS PASS with the
roster (top 5 by RSS, argv tail, the caller's own process tree marked
`(self)`); WARN ONLY when /proc/meminfo's MemAvailable drops below the
module constant; never FAIL; never sum RSS; no wall-clock window.

Drives the check against a FAKE /proc tree under tmp_path via the module's
`_PROC` seam — never the real /proc.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402

PAGE = os.sysconf('SC_PAGE_SIZE')


def _mb_to_pages(mb: float) -> int:
    return round(mb * 1024 * 1024 / PAGE)


def _write_proc_entry(proc_dir: Path, pid: int, *, rss_mb: float,
                       argv: list[str], ppid: int = 1):
    d = proc_dir / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    # statm: size resident share text lib data dt — only field[1] (resident) matters.
    resident_pages = _mb_to_pages(rss_mb)
    (d / 'statm').write_text(f'0 {resident_pages} 0 0 0 0 0\n')
    (d / 'cmdline').write_bytes(('\x00'.join(argv) + '\x00').encode())
    # stat: "pid (comm) state ppid ...". comm can contain spaces/parens in
    # real life; a plain name here is enough for these tests.
    (d / 'stat').write_text(f'{pid} (proc{pid}) S {ppid} 0 0 0 0 0\n')


def _write_meminfo(proc_dir: Path, mem_available_mb: float):
    kb = int(mem_available_mb * 1024)
    (proc_dir / 'meminfo').write_text(
        f'MemTotal:       16384000 kB\n'
        f'MemFree:         2048000 kB\n'
        f'MemAvailable:    {kb} kB\n'
    )


def _patch_proc(monkeypatch, proc_dir: Path):
    monkeypatch.setattr(doc, '_PROC', str(proc_dir))


def test_pass_with_roster_when_memory_is_healthy(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 111, rss_mb=500, argv=['python3', 'engine.py', '--date', '2026-09-14'])
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.PASS
    assert 'pid=111' in r['detail']
    assert 'MemAvailable=8000MB' in r['detail']


def test_never_warns_just_because_a_process_is_large(tmp_path, monkeypatch):
    """The old semantics WARNed whenever any process exceeded 1GB RSS —
    ruled semantics retire that: a big process alone is routine, not a
    fault. A 4GB fleet-backtest child with healthy MemAvailable must PASS."""
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 222, rss_mb=4096,
                      argv=['python3', '-m', 'backtest.unified_backtest', '--strategy-id', 'S_x'])
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.PASS


def test_warns_only_when_mem_available_below_threshold(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 333, rss_mb=100, argv=['python3', 'small.py'])
    _write_meminfo(proc, mem_available_mb=doc.CO_TENANT_MEM_AVAILABLE_WARN_MB - 1)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.WARN
    assert 'MemAvailable=' in r['detail']


def test_never_fails(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 444, rss_mb=8000, argv=['python3', 'huge.py'])
    _write_meminfo(proc, mem_available_mb=10)   # essentially out of memory
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] in (doc.PASS, doc.WARN)
    assert r['severity'] != doc.FAIL


def test_roster_is_top_5_by_rss_sorted_descending(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    sizes = [100, 700, 300, 900, 500, 200, 50]   # 7 processes
    for i, mb in enumerate(sizes, start=1000):
        _write_proc_entry(proc, i, rss_mb=mb, argv=['python3', f'p{i}.py'])
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    # Only the top 5 by RSS should appear: 900, 700, 500, 300, 200.
    for expected_mb in (900, 700, 500, 300, 200):
        assert f'{expected_mb}MB' in r['detail']
    for excluded_mb in (100, 50):
        assert f'{excluded_mb}MB pid=' not in r['detail']


def test_never_sums_rss_across_processes(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 501, rss_mb=1000, argv=['python3', 'a.py'])
    _write_proc_entry(proc, 502, rss_mb=1000, argv=['python3', 'b.py'])
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    # The old implementation reported "N co-tenant(s), {total}MB total" —
    # that summed-total phrasing must be gone.
    assert 'total' not in r['detail'].lower()


def test_own_process_tree_is_marked_self(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    my_pid = os.getpid()
    # The check's own pid, with a stat file so _co_tenant_own_process_tree
    # can walk it — its ppid (1) has no stat file in this fake tree, so the
    # walk terminates cleanly right after.
    _write_proc_entry(proc, my_pid, rss_mb=600, argv=['python3', '-m', 'pytest'], ppid=1)
    _write_proc_entry(proc, 9001, rss_mb=650, argv=['python3', 'other_tenant.py'])
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert f'pid={my_pid} (self)' in r['detail']
    assert 'pid=9001 (self)' not in r['detail']
    assert 'pid=9001' in r['detail']


def test_argv_tail_not_head_is_shown_when_truncated(tmp_path, monkeypatch):
    """A long argv must be truncated from the FRONT (keep the tail) so
    trailing flags like `--strategy-id <sid>` survive — the head is just
    the interpreter path, the least useful part to see."""
    proc = tmp_path / 'proc'
    long_argv = ['python3', '-m', 'backtest.unified_backtest',
                'x' * 90, '--strategy-id', 'S_the_important_tail']
    _write_proc_entry(proc, 601, rss_mb=600, argv=long_argv)
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert '--strategy-id S_the_important_tail' in r['detail']


def test_process_vanishing_mid_scan_is_skipped_not_a_crash(tmp_path, monkeypatch):
    """A pid directory whose statm exists but whose cmdline was already gone
    (the process exited between the glob listing and the read) must be
    silently skipped — the check must not crash and other processes must
    still be reported."""
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 701, rss_mb=500, argv=['python3', 'still_alive.py'])
    # Simulate a vanished process: statm present, cmdline missing.
    vanished_dir = proc / '702'
    vanished_dir.mkdir(parents=True)
    (vanished_dir / 'statm').write_text(f'0 {_mb_to_pages(400)} 0 0 0 0 0\n')
    # No cmdline file written for pid 702 — open() will raise FileNotFoundError.
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.PASS
    assert 'pid=701' in r['detail']
    assert 'pid=702' not in r['detail']


def test_no_processes_found_still_passes(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir(parents=True)
    _write_meminfo(proc, mem_available_mb=8000)
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.PASS


def test_unreadable_meminfo_never_crashes_and_never_warns_on_that_alone(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    _write_proc_entry(proc, 801, rss_mb=500, argv=['python3', 'x.py'])
    # No meminfo file at all.
    _patch_proc(monkeypatch, proc)

    r = doc.check_co_tenant_memory()
    assert r['severity'] == doc.PASS
