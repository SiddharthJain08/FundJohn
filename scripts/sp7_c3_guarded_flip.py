#!/usr/bin/env python3
"""SP-7 Phase C — C3 guarded auto-flip of OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE=1.

One-shot. RE-ARMED 2026-06-24 (operator: "re-arm after the next clean cycle").
The original Tue 2026-06-23 12:00 UTC fire ran + correctly ABORTED (5/8 guards
failed — Mon/Tue cycles were destroyed by the EOD-OOM crash-loop, not C1; the
calendar sweep was still running; mem < floor). That fire is spent. This re-arm
targets **Thu 2026-06-25 22:30 UTC**, observing the **Thu 06-25 16:15 ET / 20:15
UTC EOD compute** (the `eod-signal-register` cron: collect→sentiment→signals→
option_hedge → writes daily_cycle_steps/aborts logs + execution_signals).

WHY Thursday, not tonight (06-24): the EOD OOM was fixed + johnbot restarted
06-24 12:02 UTC, so tonight's 20:15 UTC compute is the FIRST post-fix scheduled
test — but today's 06-24 logs are already POISONED by a morning manual OOM
remediation run (stale daily_cycle_aborts_2026-06-24.log + rc=137 in the steps
log). Guarding on 06-24 would false-fail G4/G5 on those stale artifacts even if
tonight's compute is clean. Thu 06-25 is a clean-slate trading day (fresh logs,
no prior run) → honest single-variable observe. Cost = ~1 day, negligible for C3
(bounded delta, clamp-gated). Deleting the forensic OOM logs to un-poison 06-24
was rejected as worse than waiting.

It NEVER flips blind. It activates C3 only if every guard below passes; otherwise
it aborts, posts a Discord alert naming the failed guard + the manual one-liner,
and exits non-zero (the one-shot timer will not retry — a human looks). NOTE: if
Thursday's compute is ALSO dirty (OOM fix didn't hold), the guards abort + alert
and a human re-engages — that is correct: a 2nd dirty scheduled cycle post-fix is
a real incident deserving eyes, not a silent auto-retry.

SCOPE NOTE: the guards verify the observe-day EOD compute was HEALTHY and the
restart is memory-SAFE (no abort/OOM, sane signal count, C1 gate still set,
headroom) — this is a "safe-to-flip" gate. It does NOT prove C1's resolver was
correct: `0 fail-open` and the signals-step peak RSS are not in durable logs, and
with the clamp still ON a fail-open-to-clamp produces a near-identical signal
count. C3 is independent of C1, so this is fine.

WHY guarded (context): flipping C3 needs a johnbot restart (the sentiment step
inherits johnbot's frozen process.env — daily_cycle_node.js:53; no per-step
dotenv reload). The restart runs sync_data_ledger.py at boot; since 2026-06-26 it
uses pyarrow column-pushdown, so its peak is ~1.1 GB (measured) — down from ~2.9 GB
when it full-read options_eod. The ~1.65 GB sentiment CoverageIndex builds in the
EOD cron, NOT at boot, and does not grow while clamp=sp500 keeps the universe ⊆
sp500. On this 8 GB no-swap box the residual OOM risk is a co-resident heavy
backtest (ERR-20260621-001), which G6 bars directly. And C3 must not be coupled
into Monday's unobserved C1 first-cycle. The guards encode both safeties.

Guards (ALL must pass to flip — these are HEALTH/SAFETY signals, not a C1 proof):
  G1  Idempotent: C3 not already ACTIVE in .env (re-run safe).
  G2  C1 still live in the RUNNING johnbot (operator didn't roll C1 back).
  G3  OBSERVE_DATE (06-26) EOD compute produced a sane execution_signals count
      (no collapse/explosion; baseline 06-15..19 = 536-752, 06-24 validation = 866
      → accept 350..1500).
  G4  No daily_cycle abort file for OBSERVE_DATE (no pipeline step aborted that day).
  G5  OBSERVE_DATE steps-log: >=1 clean `step=signals rc=0`, no `step=signals` rc>=2,
      and NO rc=137 (OOM) on any step that day.
  G6  No heavy backtest/ledger process running right now (panel_one / unified_backtest
      / refresh_backtests / sync_data_ledger / --all-live).
  G7  Memory headroom: MemAvailable >= 2500 MB. Real restart peak is the boot
      sync_data_ledger pushdown (~1.1 GB, measured 2026-06-26); the ~1.65 GB
      sentiment CoverageIndex is EOD-cron not boot, and clamp-gated. 2500 = peak
      + ~1.4 GB margin, reliably met on this box (trough ~3.4 GB). Was 4500, which
      double-counted the non-concurrent sentiment build and the full-read peak.
  G8  Not inside the EOD-compute window (20:00<=now<21:45 UTC) — belt & suspenders;
      the timer fires 22:30 UTC, well clear of the 20:15 UTC EOD compute + option_hedge.

On all-pass: uncomment the .env gate (atomic, preserves 0600, NO .env.bak), restart
johnbot, verify (:3000=200 + gate present in /proc/<newpid>/environ within 90s),
post success, disable the timer, exit 0. If verify fails: re-comment the gate +
one clean restart + loud alert (don't leave a half-applied change).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/root/openclaw')
ENV_PATH = ROOT / '.env'
OBSERVE_DATE = '2026-06-26'          # Fri — re-arm target; observes the 06-26 20:15 UTC EOD compute (the 06-25 fire aborted on the old 4500 mem floor, not on a dirty cycle)
GATE = 'OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE'
GATE_LINE_RE = re.compile(r'^#\s*' + re.escape(GATE) + r'=1\s*$')
ACTIVE_LINE_RE = re.compile(r'^' + re.escape(GATE) + r'=1\s*$')
SIGNALS_MIN, SIGNALS_MAX = 350, 1500
MEM_FLOOR_MB = 2500                  # 2026-06-26: was 4500. Restart peak measured ~1.1GB after sync_data_ledger pyarrow column-pushdown (options_eod full-read 2907->939MB, prices 1538->1130MB); +1.65GB sentiment is EOD-cron/clamp-gated, not the restart. 2500 = ~1.1GB peak + ~1.4GB margin, reliably met (box trough ~3.4GB).
HEAVY_PROC_RE = re.compile(
    r'panel_one|unified_backtest|refresh_backtests|sync_data_ledger|--all-live'
)
MANUAL_ONELINER = (
    "manual flip: edit /root/openclaw/.env (uncomment OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE=1) "
    "then `systemctl --user restart johnbot` in a memory lull"
)


def log(msg: str) -> None:
    print(f"[c3-flip {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] {msg}", flush=True)


def post_discord(msg: str) -> None:
    """Best-effort Discord alert; never raises (journald is the source of truth)."""
    try:
        sys.path.insert(0, str(ROOT))
        from src.execution.pipeline_orchestrator import post_channel
        ch = os.environ.get('OPENCLAW_C3_FLIP_DISCORD_WEBHOOK_NAME', 'botjohn-log')
        post_channel(ch, msg)
        log("discord: posted")
    except Exception as e:  # noqa: BLE001
        log(f"discord post failed (non-fatal): {e}")


# ---------------------------------------------------------------- systemd / proc

def _sc_user(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', '--user', *args],
                          capture_output=True, text=True, check=check)


def johnbot_main_pid() -> int:
    r = _sc_user('show', '-p', 'MainPID', '--value', 'johnbot')
    try:
        return int((r.stdout or '0').strip())
    except ValueError:
        return 0


def proc_environ(pid: int) -> dict:
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for chunk in raw.split(b'\0'):
        if b'=' in chunk:
            k, _, v = chunk.partition(b'=')
            out[k.decode('utf-8', 'replace')] = v.decode('utf-8', 'replace')
    return out


def mem_available_mb() -> int:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def heavy_proc_running() -> str | None:
    r = subprocess.run(['ps', '-eo', 'pid,args'], capture_output=True, text=True)
    me = os.getpid()
    for line in r.stdout.splitlines():
        if 'sp7_c3_guarded_flip' in line:      # ignore self
            continue
        if HEAVY_PROC_RE.search(line):
            parts = line.split(None, 1)
            try:
                if int(parts[0]) == me:
                    continue
            except (ValueError, IndexError):
                pass
            return line.strip()[:160]
    return None


# ---------------------------------------------------------------- guard sources

def observe_signal_count() -> int:
    """execution_signals rows the OBSERVE_DATE EOD compute wrote (created_at::date)."""
    import psycopg2
    uri = os.environ['POSTGRES_URI']
    with psycopg2.connect(uri) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM execution_signals WHERE created_at::date = %s",
            (OBSERVE_DATE,),
        )
        return int(cur.fetchone()[0])


def abort_file_exists() -> bool:
    return (ROOT / 'logs' / f'daily_cycle_aborts_{OBSERVE_DATE}.log').exists()


def steps_log_check() -> tuple[bool, str]:
    """Return (ok, detail). ok=True iff >=1 `step=signals rc=0`, no signals rc>=2,
    and no rc=137 (OOM) on any step on OBSERVE_DATE."""
    p = ROOT / 'logs' / f'daily_cycle_steps_{OBSERVE_DATE}.log'
    if not p.exists():
        return False, "steps-log missing (no cycle ran?)"
    txt = p.read_text(errors='replace')
    headers = re.findall(r'step=(\w+) rc=(-?\d+)', txt)
    if not headers:
        return False, "no step headers in steps-log"
    sig_rcs = [int(rc) for s, rc in headers if s == 'signals']
    if not any(rc == 0 for rc in sig_rcs):
        return False, f"no clean signals step (signals rc seen: {sig_rcs or 'none'})"
    if any(rc >= 2 for rc in sig_rcs):
        return False, f"signals step aborted (rc>=2): {sig_rcs}"
    oom = [(s, rc) for s, rc in headers if int(rc) == 137]
    if oom:
        return False, f"OOM (rc=137) on steps: {oom}"
    # Soft: if the C1 success line survived into the stdout tail, require 0 fail-open.
    m = re.search(r'live-universe ON: union \d+ tickers, \d+ strategies, (\d+) fail-open', txt)
    if m and int(m.group(1)) > 0:
        return False, f"C1 logged {m.group(1)} fail-open strategies (not clean)"
    foundp = f"; live-universe line: {'0 fail-open OK' if m else 'not in stdout-tail (neutral)'}"
    return True, f"signals rc=0 ok{foundp}"


# ---------------------------------------------------------------- .env mutation

def gate_state() -> str:
    """'active' | 'staged' | 'missing'."""
    for line in ENV_PATH.read_text().splitlines():
        if ACTIVE_LINE_RE.match(line):
            return 'active'
    for line in ENV_PATH.read_text().splitlines():
        if GATE_LINE_RE.match(line):
            return 'staged'
    return 'missing'


def _rewrite_env(transform) -> None:
    """Atomically rewrite .env applying ``transform(line)->line``; preserve 0600."""
    orig_mode = ENV_PATH.stat().st_mode & 0o777
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    new = [transform(l) for l in lines]
    tmp = ENV_PATH.with_suffix('.env.tmp')   # NOT .env.bak (world-readable leak risk)
    tmp.write_text(''.join(new))
    os.chmod(tmp, orig_mode)
    os.replace(tmp, ENV_PATH)


def uncomment_gate() -> None:
    _rewrite_env(lambda l: (GATE + '=1\n') if GATE_LINE_RE.match(l.rstrip('\n')) else l)


def recomment_gate() -> None:
    _rewrite_env(lambda l: ('# ' + GATE + '=1\n') if ACTIVE_LINE_RE.match(l.rstrip('\n')) else l)


# ---------------------------------------------------------------- restart/verify

def restart_and_verify(timeout_s: int = 90) -> tuple[bool, str]:
    old = johnbot_main_pid()
    log(f"restarting johnbot (old MainPID={old}) ...")
    r = _sc_user('restart', 'johnbot')
    if r.returncode != 0:
        return False, f"restart cmd rc={r.returncode}: {r.stderr.strip()[:200]}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        pid = johnbot_main_pid()
        if pid == 0 or pid == old:
            continue
        active = _sc_user('is-active', 'johnbot').stdout.strip()
        if active != 'active':
            continue
        http = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
             'http://localhost:3000/'], capture_output=True, text=True).stdout.strip()
        env_ok = proc_environ(pid).get(GATE) == '1'
        if active == 'active' and http == '200' and env_ok:
            return True, f"new MainPID={pid}, :3000=200, {GATE}=1 in process env"
    pid = johnbot_main_pid()
    env = proc_environ(pid)
    return False, (f"verify timeout: pid={pid} active="
                   f"{_sc_user('is-active','johnbot').stdout.strip()} "
                   f"{GATE}={env.get(GATE)}")


# ---------------------------------------------------------------- main

def main() -> int:
    log(f"SP-7 C3 guarded flip — observe_date={OBSERVE_DATE}")

    # G1 — idempotent
    st = gate_state()
    if st == 'active':
        log("G1: gate already ACTIVE in .env — nothing to do (idempotent exit).")
        return 0
    if st == 'missing':
        post_discord(f"⚠️ SP-7 C3 flip ABORTED: `{GATE}` line missing from .env (not staged). {MANUAL_ONELINER}")
        log("G1 FAIL: gate line not found in .env (neither active nor staged).")
        return 1
    log("G1 ok: gate is staged (commented), not yet active.")

    failures: list[str] = []
    notes: list[str] = []

    # G8 — not inside the EOD-compute window (20:15 UTC + collect/sentiment/signals/option_hedge)
    now = datetime.now(timezone.utc)
    mins = now.hour * 60 + now.minute
    if 20 * 60 <= mins < 21 * 60 + 45:
        failures.append("G8 inside EOD-compute window (20:00-21:45 UTC)")

    # G2 — C1 still live in running johnbot
    pid = johnbot_main_pid()
    c1 = proc_environ(pid).get('OPENCLAW_LIVE_UNIVERSE_RESOLVER')
    if c1 == '1':
        notes.append(f"G2 ok: C1 live (johnbot PID {pid})")
    else:
        failures.append(f"G2 C1 NOT live in johnbot (OPENCLAW_LIVE_UNIVERSE_RESOLVER={c1}) — was C1 rolled back?")

    # G3 — observe-day EOD-compute signal count sane
    try:
        n = observe_signal_count()
        if SIGNALS_MIN <= n <= SIGNALS_MAX:
            notes.append(f"G3 ok: {OBSERVE_DATE} signals={n} (baseline 536-866)")
        else:
            failures.append(f"G3 {OBSERVE_DATE} signals={n} out of sane range [{SIGNALS_MIN},{SIGNALS_MAX}]")
    except Exception as e:  # noqa: BLE001
        failures.append(f"G3 signal-count query failed: {e}")

    # G4 — no abort file
    if abort_file_exists():
        failures.append(f"G4 abort file exists: daily_cycle_aborts_{OBSERVE_DATE}.log")
    else:
        notes.append("G4 ok: no abort file")

    # G5 — steps-log signals clean
    ok5, detail5 = steps_log_check()
    (notes if ok5 else failures).append(("G5 ok: " if ok5 else "G5 ") + detail5)

    # G6 — no heavy proc
    hp = heavy_proc_running()
    if hp:
        failures.append(f"G6 heavy process running: {hp}")
    else:
        notes.append("G6 ok: no heavy backtest/ledger process")

    # G7 — memory headroom
    avail = mem_available_mb()
    if avail >= MEM_FLOOR_MB:
        notes.append(f"G7 ok: MemAvailable={avail}MB (>= {MEM_FLOOR_MB})")
    else:
        failures.append(f"G7 MemAvailable={avail}MB < {MEM_FLOOR_MB}MB floor")

    for ln in notes:
        log(ln)
    for ln in failures:
        log("FAIL " + ln)

    if failures:
        post_discord(
            "⚠️ **SP-7 C3 auto-flip ABORTED** — guards failed, C3 NOT flipped:\n"
            + "\n".join(f"  · {f}" for f in failures)
            + f"\nPassed: {len(notes)} guard(s). {MANUAL_ONELINER}"
        )
        log(f"ABORT: {len(failures)} guard(s) failed; C3 left staged.")
        return 1

    # ---- all guards passed: flip ----
    log("ALL GUARDS PASS — flipping C3.")
    uncomment_gate()
    if gate_state() != 'active':
        post_discord(f"⚠️ SP-7 C3 flip: .env uncomment did not take effect. {MANUAL_ONELINER}")
        log("ABORT: post-edit gate_state != active.")
        return 1
    log("G: .env gate uncommented (active).")

    ok, detail = restart_and_verify()
    if ok:
        _sc_user('disable', 'sp7-c3-flip.timer')   # one-shot: prevent re-fire
        post_discord(
            "✅ **SP-7 Phase C C3 FLIPPED LIVE** — `OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE=1`.\n"
            f"  · {detail}\n"
            "  · First clean single-variable C3 run = next EOD compute "
            "(Fri 2026-06-26 20:15 UTC; watch `sentiment: resolver universe +N`).\n"
            "  · Rollback: re-comment line + `systemctl --user restart johnbot`."
        )
        log(f"SUCCESS: C3 live. {detail}")
        return 0

    # verify failed — revert the half-applied change + one clean restart, then alert loud
    log(f"VERIFY FAILED: {detail} — reverting C3 + one clean restart.")
    recomment_gate()
    ok2, detail2 = restart_and_verify(timeout_s=120)
    post_discord(
        "🔴 **SP-7 C3 flip FAILED verify — REVERTED C3** and "
        + ("recovered johnbot." if ok2 else "johnbot STILL UNHEALTHY — CHECK NOW.")
        + f"\n  · flip verify: {detail}\n  · recovery: {detail2}\n  · {MANUAL_ONELINER}"
    )
    log(f"REVERTED. recovery_ok={ok2} {detail2}")
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — never die silently; alert + non-zero
        log(f"UNEXPECTED ERROR: {e!r}")
        post_discord(f"🔴 SP-7 C3 auto-flip crashed: {e!r}. C3 left as-is. {MANUAL_ONELINER}")
        sys.exit(2)
