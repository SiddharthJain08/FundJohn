"""system_checks: fmp_provider_health (2026-08-23).

Reads the last-24h FMP rows in data_provider_health. Two failure shapes:
an error-ratio spike (quota / 429 / transport) and SILENCE on a weekday —
the collector's fundamentals phase halted at ticker ~30 for ten days and
nothing noticed because FMP recorded nothing. Tier-gated symbols and 404s
are recorded as successes upstream, so they never trip this.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.system_checks.checks import fmp_provider_health as chk
from src.system_checks.types import Status

MON = datetime(2026, 8, 24, 22, 0, tzinfo=timezone.utc)   # Monday, after the 16:15 ET collect
SUN = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _row(endpoint, ok, err, last_error=None):
    return {'endpoint': endpoint, 'success_count': ok, 'error_count': err, 'last_error': last_error}


def test_pass_when_healthy_volume():
    st, detail = chk.evaluate([_row('income_statement', 1400, 3), _row('insider_trading_latest', 6, 0)], now=MON)
    assert st is Status.PASS
    assert '1406 ok' in detail and '3 err' in detail


def test_warn_on_error_ratio_spike():
    st, detail = chk.evaluate([_row('income_statement', 60, 40, 'HTTP 429: Too Many Requests')], now=MON)
    assert st is Status.WARN
    assert '40.0%' in detail and 'HTTP 429' in detail


def test_fail_when_quota_dominates():
    st, detail = chk.evaluate([_row('income_statement', 5, 95, 'HTTP 402: Limit Reach')], now=MON)
    assert st is Status.FAIL


def test_warn_on_weekday_silence_but_pass_on_weekend():
    st, detail = chk.evaluate([], now=MON)
    assert st is Status.WARN and 'no FMP calls' in detail
    st, _ = chk.evaluate([], now=SUN)
    assert st is Status.PASS


def test_small_samples_do_not_alarm():
    st, _ = chk.evaluate([_row('quote', 2, 3, 'HTTP 500')], now=MON)   # 5 calls: too few to judge a ratio
    assert st is Status.PASS
