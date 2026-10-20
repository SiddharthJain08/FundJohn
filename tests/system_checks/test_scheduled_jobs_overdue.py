"""scheduled_jobs_overdue check (E2): pure evaluate() + policy tables + drop-in guards.

Never touches Postgres: evaluate() is pure; the @check body is exercised only via mocks.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src.system_checks.checks import scheduled_jobs as chk
from src.system_checks.types import Status

ROOT = Path(__file__).resolve().parents[2]
MON_1000_ET = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
FRI_1600_ET = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)


def _row(job, last_ok, fails=0):
    return {'job_name': job, 'source': 'cron', 'last_success_at': last_ok,
            'n_failures_since_success': fails, 'last_note': None}


def test_session_job_monday_after_friday_passes():
    assert chk.evaluate([_row('sameday-compute-1500', FRI_1600_ET)], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.PASS


def test_session_job_missed_monday_warns_tuesday():
    st, d = chk.evaluate([_row('sameday-compute-1500', FRI_1600_ET)], chk.CRON_POLICIES,
                         now=MON_1000_ET + timedelta(days=1))
    assert st is Status.WARN and 'sameday-compute-1500' in d


def test_holiday_not_counted_as_missed():
    wed = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)   # Wed 16:00 ET
    fri = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)   # Fri 10:00 ET; Thu = Thanksgiving
    assert chk.evaluate([_row('sameday-compute-1500', wed)], chk.CRON_POLICIES, now=fri)[0] is Status.PASS


def test_hours_policy_overdue_after_2x():
    assert chk.evaluate([_row('crypto-regime-tick', MON_1000_ET - timedelta(hours=2, minutes=1))],
                        chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN
    assert chk.evaluate([_row('crypto-regime-tick', MON_1000_ET - timedelta(hours=1, minutes=59))],
                        chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.PASS


def test_intraday_not_judged_outside_session():
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert chk.evaluate([_row('circuit-breaker-tick', FRI_1600_ET)], chk.CRON_POLICIES, now=sat)[0] is Status.PASS


def test_intraday_judged_inside_session():
    stale = MON_1000_ET - timedelta(minutes=11)
    assert chk.evaluate([_row('circuit-breaker-tick', stale)], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN
    # session only just opened (09:35 ET): not yet judged
    early = datetime(2026, 10, 19, 13, 35, tzinfo=timezone.utc)
    assert chk.evaluate([_row('circuit-breaker-tick', FRI_1600_ET)], chk.CRON_POLICIES, now=early)[0] is Status.PASS


def test_failures_without_success_warn():
    assert chk.evaluate([_row('premarket-gate-0915', None, fails=3)], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN


def test_unseen_and_unpolicied_jobs_never_warn():
    st, d = chk.evaluate([_row('openclaw-rf-flip.service', None, fails=0)], {}, now=MON_1000_ET)
    assert st is Status.PASS and '1 unpolicied' in d


def test_no_rows_warns():
    st, d = chk.evaluate([], chk.CRON_POLICIES, now=MON_1000_ET)
    assert st is Status.WARN and 'wiring inactive' in d


def test_warn_lists_at_most_four_names():
    rows = [_row(n, None, fails=1) for n in ['sameday-compute-1500', 'sameday-execute-1555', 'premarket-gate-0915',
                                             'opg-sweep-0932', 'open-reconcile-0925', 'morning-regime-0900']]
    st, d = chk.evaluate(rows, chk.CRON_POLICIES, now=MON_1000_ET)
    assert st is Status.WARN and d.count('-') >= 4 and 'morning-regime-0900' not in d


def test_policy_from_oncalendar():
    assert chk.policy_from_oncalendar(['Mon..Fri *-*-* 16:15:00 America/New_York']) == ('session', 1)
    assert chk.policy_from_oncalendar(['Sat *-*-* 06:00:00 UTC']) == ('hours', 168)
    assert chk.policy_from_oncalendar(['*-*-* 06:00:00 UTC']) == ('hours', 24)
    assert chk.policy_from_oncalendar(['Tue..Fri *-*-* 21:45:00 UTC']) is None
    assert chk.policy_from_oncalendar(['Sat *-*-* 06:00:00 UTC', 'Mon..Fri *-*-* 06:00:00 UTC']) is None


def test_systemd_policies_keys_template_instance(tmp_path):
    (tmp_path / 'openclaw-zz.timer').write_text(
        '[Timer]\nOnCalendar=Mon..Fri *-*-* 07:15:00 UTC\nUnit=openclaw-zz@0715.service\n')
    (tmp_path / 'openclaw-yy.timer').write_text('[Timer]\nOnCalendar=Sat *-*-* 06:00:00 UTC\n')
    (tmp_path / 'openclaw-xx.timer').write_text('[Timer]\nOnCalendar=Tue..Fri *-*-* 21:45:00 UTC\n')
    pol = chk.systemd_policies(tmp_path)
    assert pol == {'openclaw-zz@0715.service': ('session', 1), 'openclaw-yy.service': ('hours', 168)}


def test_cron_policy_keys_equal_cron_schedule_names():
    names = set(re.findall(r"_sched\('([a-z0-9-]+)'", (ROOT / 'src/engine/cron-schedule.js').read_text()))
    assert names == set(chk.CRON_POLICIES) and len(names) == 26


def test_installer_never_installs_job_stat_dropin():
    sh = (ROOT / 'scripts/install_systemd.sh').read_text()
    assert 'job-stat' not in sh and '"$SRC"/*.service.d' in sh
    assert (ROOT / 'docs/systemd/job-stat/job-stat.conf').exists()
    assert not list((ROOT / 'docs/systemd').glob('*.service.d/job-stat.conf'))


def test_dropin_uses_startpre_and_stoppost_with_dash():
    conf = (ROOT / 'docs/systemd/job-stat/job-stat.conf').read_text()
    assert 'ExecStartPre=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py start %n' in conf
    assert 'ExecStopPost=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py finish %n' in conf
    assert 'ExecStartPost' not in conf


def test_check_body_warns_when_table_absent_via_mocks(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x/y')
    cur = mock.MagicMock()
    cur.fetchone.return_value = (None,)
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    fake = mock.MagicMock()
    fake.connect.return_value = conn
    monkeypatch.setitem(sys.modules, 'psycopg2', fake)
    st, d = chk._scheduled_jobs_overdue()
    assert st is Status.WARN and 'migration 167' in d
