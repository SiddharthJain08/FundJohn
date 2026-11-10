"""Cron wiring for the crypto breaker tick: source read, no node runtime needed."""
from __future__ import annotations

from pathlib import Path

CRON = Path(__file__).resolve().parents[2] / 'src' / 'engine' / 'cron-schedule.js'


def _block() -> str:
    js = CRON.read_text()
    assert js.count("_sched('crypto-breaker-tick'") == 1
    start = js.index("_sched('crypto-breaker-tick'")
    end = js.index("}, { timezone: 'UTC' });", start)
    return js[start:end]


def test_job_name_and_five_minute_utc_schedule():
    b = _block()
    assert "'*/5 * * * *'" in b


def test_gated_on_tick_flag_only():
    b = _block()
    assert "process.env.OPENCLAW_CRYPTO_BREAKER_TICK !== '1'" in b
    assert "OPENCLAW_CRYPTO_BREAKER " not in b.replace("OPENCLAW_CRYPTO_BREAKER_", "")  # arming is the script's business


def test_spawn_under_timeout_and_memory_cap():
    b = _block()
    assert "'src/execution/crypto_breaker.py'" in b
    assert "'timeout'" in b and "'240'" in b and "'--kill-after=15'" in b
    assert "'400M'" in b and 'OPENCLAW_CRYPTO_BREAKER_MEMORY_MAX' in b
    assert 'crypto_breaker_' in b and 'closeSync(logFd)' in b


def test_utc_timezone_declared():
    js = CRON.read_text()
    start = js.index("_sched('crypto-breaker-tick'")
    assert "{ timezone: 'UTC' }" in js[start:start + 2500]
