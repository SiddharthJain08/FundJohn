"""NYSE session calendar — master-first, never silent.

Master: data/master/trading_calendar.parquet (built by
src/ingestion/ingest_trading_calendar.py from `alpaca calendar`, which serves
every session 1970–2029 including exchange-declared closures). Rows carry
`active`; a session the exchange later cancels is kept with active=false
(append-only invariant).

Resolution order for every query:
  1. the master (cached per file mtime);
  2. one `alpaca calendar --start --end` call for the requested range;
  3. weekday arithmetic, with a WARNING (holidays are NOT modelled there).
"""
from __future__ import annotations

import bisect
import datetime as dt
import functools
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH_ENV = 'OPENCLAW_TRADING_CALENDAR_PATH'
DEFAULT_MASTER = ROOT / 'data' / 'master' / 'trading_calendar.parquet'
_ALPACA_BIN = os.environ.get('ALPACA_CLI', '/root/go/bin/alpaca')
_FALLBACK_WARNED = False


def master_path() -> Path:
    return Path(os.environ.get(MASTER_PATH_ENV) or DEFAULT_MASTER)


def _as_date(d) -> dt.date:
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    if hasattr(d, 'to_pydatetime'):          # pandas.Timestamp
        return d.to_pydatetime().date()
    return dt.date.fromisoformat(str(d)[:10])


class _Calendar:
    def __init__(self, hours: dict[dt.date, tuple[dt.time, dt.time]]):
        self.hours = hours
        self.dates: list[dt.date] = sorted(hours)
        self.first = self.dates[0] if self.dates else None
        self.last = self.dates[-1] if self.dates else None

    def covers(self, d: dt.date) -> bool:
        return self.first is not None and self.first <= d <= self.last


def _parse_hhmm(s) -> dt.time:
    s = str(s)
    if ':' in s:
        h, m = s.split(':')[:2]
    else:
        h, m = s[:2], s[2:4]
    return dt.time(int(h), int(m))


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> _Calendar:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['date', 'open', 'close', 'active'])
    hours: dict[dt.date, tuple[dt.time, dt.time]] = {}
    for row in tbl.to_pylist():
        if row.get('active') is False:
            continue
        d = _as_date(row['date'])
        hours[d] = (_parse_hhmm(row.get('open') or '09:30'), _parse_hhmm(row.get('close') or '16:00'))
    return _Calendar(hours)


def _calendar() -> _Calendar | None:
    p = master_path()
    if not p.exists():
        return None
    try:
        return _load(str(p), p.stat().st_mtime_ns)
    except Exception as exc:  # noqa: BLE001 — a corrupt master must not take the engine down
        log.warning('trading_calendar: master %s unreadable (%s)', p, exc)
        return None


def clear_cache() -> None:
    _load.cache_clear()
    global _FALLBACK_WARNED
    _FALLBACK_WARNED = False


def _alpaca_sessions(start: dt.date, end: dt.date) -> set[dt.date] | None:
    """Sessions in [start, end] from the alpaca CLI, or None when the probe fails."""
    try:
        r = subprocess.run([_ALPACA_BIN, 'calendar', '--start', start.isoformat(),
                            '--end', end.isoformat()],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout) if r.stdout.strip() else []
        return {dt.date.fromisoformat(x['date']) for x in rows if isinstance(x, dict) and x.get('date')}
    except Exception:  # noqa: BLE001
        return None


def _warn_fallback() -> None:
    global _FALLBACK_WARNED
    if not _FALLBACK_WARNED:
        log.warning('trading_calendar: master missing/out of range at %s and alpaca probe failed — '
                    'weekday fallback (holidays NOT modelled)', master_path())
        _FALLBACK_WARNED = True


def _sessions_any(start: dt.date, end: dt.date) -> list[dt.date]:
    """Sessions in [start, end] by the resolution order."""
    cal = _calendar()
    if cal is not None and cal.covers(start) and cal.covers(end):
        lo = bisect.bisect_left(cal.dates, start)
        hi = bisect.bisect_right(cal.dates, end)
        return cal.dates[lo:hi]
    probe = _alpaca_sessions(start, end)
    if probe is not None:
        return sorted(d for d in probe if start <= d <= end)
    _warn_fallback()
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def is_session(d) -> bool:
    d = _as_date(d)
    return d in _sessions_any(d, d)


def sessions(start, end) -> list[dt.date]:
    return _sessions_any(_as_date(start), _as_date(end))


def next_session(d) -> dt.date:
    d = _as_date(d)
    found = _sessions_any(d + dt.timedelta(days=1), d + dt.timedelta(days=14))
    if found:
        return found[0]
    _warn_fallback()
    n = d + dt.timedelta(days=1)
    while n.weekday() >= 5:
        n += dt.timedelta(days=1)
    return n


def prev_session(d) -> dt.date:
    d = _as_date(d)
    found = _sessions_any(d - dt.timedelta(days=14), d - dt.timedelta(days=1))
    if found:
        return found[-1]
    _warn_fallback()
    p = d - dt.timedelta(days=1)
    while p.weekday() >= 5:
        p -= dt.timedelta(days=1)
    return p


def sessions_before(d, n: int) -> list[dt.date]:
    d = _as_date(d)
    span = max(14, int(n * 1.6) + 7)
    found = _sessions_any(d - dt.timedelta(days=span), d - dt.timedelta(days=1))
    while len(found) < n and span < 5000:
        span *= 2
        found = _sessions_any(d - dt.timedelta(days=span), d - dt.timedelta(days=1))
    return found[-n:] if n > 0 else []


def expiry_session(d) -> dt.date:
    d = _as_date(d)
    return d if is_session(d) else prev_session(d)


def is_open(now_et: dt.datetime) -> bool:
    """Regular trading hours on a session day, honouring the master's early closes."""
    d = now_et.date()
    if not is_session(d):
        return False
    cal = _calendar()
    o, c = (cal.hours.get(d) if cal is not None else None) or (dt.time(9, 30), dt.time(16, 0))
    t = now_et.time().replace(tzinfo=None)
    return o <= t < c
