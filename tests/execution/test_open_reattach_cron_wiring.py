"""Operator decision 2026-11-23 (option 2): fractional LONG positions can only
carry a simple DAY protective stop (Alpaca rejects fractional GTC/OCO), and the
two stop_reattach passes ran at ~10:25 ET and ~15:58 ET — so longs had no
broker-side stop from 16:00 until ~10:25 the next day. A third pass right after
the 09:30 ET open shrinks that to the overnight gap. Source-read assertions on
the scheduler (no node runtime, no Discord), same style as
test_account_breaker_cron_wiring.py."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRON = ROOT / 'src' / 'engine' / 'cron-schedule.js'


def _block():
    js = CRON.read_text()
    m = re.search(r"_sched\('sameday-open-reattach-0936',\s*'([^']+)'", js)
    assert m, 'sameday-open-reattach-0936 schedule missing'
    start = m.start()
    end = js.index("}, { timezone: 'America/New_York' });", start)
    return m.group(1), js[start:end]


def test_open_reattach_runs_at_0936_et_weekdays():
    expr, _ = _block()
    assert expr == '36 9 * * 1-5'


def test_open_reattach_dispatches_only_the_stop_reattach_step():
    _, block = _block()
    assert "dispatchCycle('sameday-open-reattach', ['stop_reattach'])" in block


def test_open_reattach_has_a_kill_switch_default_on():
    _, block = _block()
    assert "OPENCLAW_OPEN_REATTACH" in block and "!== '0'" in block


def test_open_reattach_lives_in_the_sameday_block_only():
    js = CRON.read_text()
    assert js.count("sameday-open-reattach-0936") == 1
    # it sits after the same-day dispatchCycle helper and before the 15:00 compute
    assert js.index("const dispatchCycle = (reason, steps) => {", js.index("sameDayExec")) \
        < js.index("sameday-open-reattach-0936") < js.index("sameday-compute-1500")
