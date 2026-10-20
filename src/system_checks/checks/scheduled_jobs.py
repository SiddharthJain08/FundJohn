"""scheduled_jobs_overdue (E2): did every job that reports to scheduled_job_runs run on schedule?

Rows come from src/lib/job_stat.{py,js} (migration 167): in-process crons
(source='cron', always on) and systemd units (source='systemd', only once the
operator installs docs/systemd/job-stat/job-stat.conf). Jobs never seen in 30
days are not judged, so mode-gated crons that are never registered are not
wallpaper. Cron rows record "dispatched/finished", so this detects "never fired".

Policy kinds:
  ('session', 1)      once per NYSE session: WARN when a full session with no success
                      lies strictly between last_success.date() and today (ET).
  ('hours', h)        WARN when now - last_success > 2*h hours.
  ('intraday_min', m) judged only while the market is open and has been open >= 2*m
                      minutes; WARN when now - last_success > 2*m minutes.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from ..registry import check
from ..types import Status

Policy = tuple  # (kind: str, value: float)

ROOT = Path(__file__).resolve().parents[3]
_ET = ZoneInfo('America/New_York')
_MAX_NAMES = 4

CRON_POLICIES: dict[str, Policy] = {
    'token-budget-reset': ('hours', 24),
    'intraday-hmm-refit-1800': ('hours', 24),
    'cadence-recompute-2355': ('hours', 24),
    'curator-priors-snapshot-0305': ('hours', 24),
    'crypto-regime-tick': ('hours', 1),
    'weekly-maintenance-sun0800': ('hours', 168),
    'intraday-hmm-tick': ('intraday_min', 15),
    'circuit-breaker-tick': ('intraday_min', 5),
    **{n: ('session', 1) for n in (
        'legacy-daily-cycle-1000', 'closeexec-compute-1510', 'closeexec-execute-1555',
        'closeexec-sod-refresh-0935', 'eod-signal-register-1615', 'premarket-news-0905',
        'premarket-gate-0915', 'open-reconcile-0925', 'opg-sweep-0932', 'eod-into-close-fill-1555',
        'tier1-acting-ingest-1430', 'sameday-compute-1500', 'sameday-execute-1555',
        'sameday-eod-collect-1615', 'sameday-evening-marks-1620', 'sameday-sod-refresh-0935',
        'morning-regime-0900', 'options-aggregates-refresh-0605')},
}

_DAY = r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)'
_WEEKDAY_RE = re.compile(rf'^{_DAY}(?:\.\.{_DAY})?(?:,{_DAY}(?:\.\.{_DAY})?)*$', re.I)


def _line_policy(line: str) -> Policy | None:
    toks = line.strip().split()
    if not toks:
        return None
    wd = None
    if _WEEKDAY_RE.match(toks[0]):
        wd, toks = toks[0], toks[1:]
    if wd is not None:
        w = wd.lower()
        if w == 'mon..fri':
            return ('session', 1)
        if w in ('sat', 'sun'):
            return ('hours', 168)
        return None
    if toks and toks[0] == '*-*-*':
        # sub-daily wildcards in the hour field (e.g. *:00:00) are not a daily job
        time_tok = toks[1] if len(toks) > 1 else '00:00:00'
        if not re.match(r'^\d{1,2}:', time_tok):
            return None
        return ('hours', 24)
    if toks and toks[0].lower() == 'daily':
        return ('hours', 24)
    return None


def policy_from_oncalendar(lines: list[str]) -> Policy | None:
    """None = unpolicied (listed in the detail, never WARN)."""
    pols = {_line_policy(ln) for ln in lines}
    if len(pols) != 1:
        return None
    return next(iter(pols))


def systemd_policies(systemd_dir: Path) -> dict[str, Policy]:
    out: dict[str, Policy] = {}
    for timer in sorted(Path(systemd_dir).glob('*.timer')):
        try:
            text = timer.read_text()
        except OSError:
            continue
        cal = [m.group(1).strip() for m in re.finditer(r'^\s*OnCalendar=(.+)$', text, re.M)]
        unit = re.search(r'^\s*Unit=(\S+)', text, re.M)
        pol = policy_from_oncalendar(cal) if cal else None
        if pol is not None:
            out[unit.group(1) if unit else timer.stem + '.service'] = pol
    return out


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=dt.timezone.utc)


def _tc():
    try:
        from lib import trading_calendar as tc
    except ImportError:
        from src.lib import trading_calendar as tc
    return tc


def _session_overdue(last_ok: dt.datetime, now_et: dt.datetime) -> bool:
    tc = _tc()
    d0, d1 = last_ok.astimezone(_ET).date(), now_et.date()
    if d1 <= d0:
        return False
    return any(d0 < s < d1 for s in tc.sessions(d0 + dt.timedelta(days=1), d1 - dt.timedelta(days=1)))


def _intraday_judged(m: float, now_et: dt.datetime) -> bool:
    tc = _tc()
    if not tc.is_open(now_et):
        return False
    opened = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return now_et - opened >= dt.timedelta(minutes=2 * m)


def _judge(row: dict, pol: Policy, now: dt.datetime, now_et: dt.datetime) -> bool:
    """True = problem."""
    kind, val = pol
    last_ok = row.get('last_success_at')
    if kind == 'intraday_min' and not _intraday_judged(val, now_et):
        return False
    if last_ok is None:
        return (row.get('n_failures_since_success') or 0) > 0
    last_ok = _aware(last_ok)
    if kind == 'session':
        return _session_overdue(last_ok, now_et)
    if kind == 'hours':
        return now - last_ok > dt.timedelta(hours=2 * val)
    if kind == 'intraday_min':
        return now - last_ok > dt.timedelta(minutes=2 * val)
    return False


def evaluate(rows: list[dict], policies: dict[str, Policy], *, now: dt.datetime):
    if not rows:
        return Status.WARN, 'no scheduled_job_runs rows in 30d — job_stat wiring inactive'
    now = _aware(now)
    now_et = now.astimezone(_ET)
    bad: list[str] = []
    n_ok = n_unpol = 0
    for r in rows:
        pol = policies.get(r['job_name'])
        if pol is None:
            n_unpol += 1
        elif _judge(r, pol, now, now_et):
            bad.append(r['job_name'])
        else:
            n_ok += 1
    if bad:
        more = f' (+{len(bad) - _MAX_NAMES} more)' if len(bad) > _MAX_NAMES else ''
        return Status.WARN, f'{len(bad)} overdue/failing: {", ".join(bad[:_MAX_NAMES])}{more}'
    return Status.PASS, f'{n_ok} on schedule, {n_unpol} unpolicied, {len(rows)} seen in 30d'


_QUERY = """
SELECT job_name, source,
       MAX(finished_at) FILTER (WHERE success) AS last_success_at,
       COUNT(*) FILTER (WHERE success IS FALSE
                          AND started_at > COALESCE(
                              (SELECT MAX(r2.finished_at) FROM scheduled_job_runs r2
                                WHERE r2.job_name = r.job_name AND r2.success), '-infinity')) AS n_fail,
       (ARRAY_AGG(note ORDER BY started_at DESC))[1] AS last_note
  FROM scheduled_job_runs r
 WHERE started_at >= NOW() - interval '30 days'
 GROUP BY job_name, source
"""


@check(name='scheduled_jobs_overdue', tags=['maintenance'], requires=['db'])
def _scheduled_jobs_overdue():
    import psycopg2
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    conn = psycopg2.connect(uri, connect_timeout=5)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET statement_timeout='30s'")
        cur.execute("SELECT to_regclass('public.scheduled_job_runs')")
        if cur.fetchone()[0] is None:
            return Status.WARN, 'migration 167 not applied'
        cur.execute(_QUERY)
        rows = [{'job_name': r[0], 'source': r[1], 'last_success_at': r[2],
                 'n_failures_since_success': int(r[3] or 0), 'last_note': r[4]}
                for r in cur.fetchall()]
    finally:
        conn.close()
    policies = CRON_POLICIES | systemd_policies(ROOT / 'docs/systemd')
    return evaluate(rows, policies, now=dt.datetime.now(dt.timezone.utc))
