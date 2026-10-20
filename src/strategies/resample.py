"""Session-aligned resampling on the NYSE calendar.

pandas ``resample('W-FRI')`` / ``'ME'`` label a bucket by a calendar date that
may not be a session (Good Friday, month-end holiday). ``session_bars`` labels
each bucket by its last NYSE session instead (``lib.trading_calendar``).
"""
from __future__ import annotations

import pandas as pd

from lib.trading_calendar import next_session, prev_session, sessions

__all__ = ['session_bars', 'next_session', 'prev_session']

_FREQ = {'W': 'W-SUN', 'M': 'M', 'Q': 'Q-DEC'}


def session_bars(wide: pd.DataFrame, rule: str, *, label: str = 'last_session',
                 fill: str = 'none') -> pd.DataFrame:
    """Resample ``wide`` (DatetimeIndex rows) to W/M/Q bars labelled on a session.

    Per bucket (pandas periods W-SUN / M / Q-DEC) the label is the last NYSE
    session <= ``wide.index.max()``; a bucket with none is omitted. Each column
    takes its last non-NaN value among rows in [bucket_start, label]; rows after
    the label (e.g. a Saturday crypto row) never contribute, and the label is
    never later than the data end.

    ``fill='prev'`` forward-fills across output rows: use it for STATE series
    only, never prices (it would fabricate a stale price bar).
    """
    if rule not in _FREQ:
        raise ValueError(f'rule must be one of {sorted(_FREQ)}, got {rule!r}')
    if label != 'last_session':
        raise ValueError(f"label must be 'last_session', got {label!r}")
    if fill not in ('none', 'prev'):
        raise ValueError(f"fill must be 'none' or 'prev', got {fill!r}")
    if not isinstance(wide.index, pd.DatetimeIndex):
        raise ValueError('wide must have a DatetimeIndex')

    out_idx = pd.DatetimeIndex([], name='date')
    if wide.empty:
        return wide.iloc[0:0].set_axis(out_idx, axis=0)

    w = wide.sort_index()
    dates = w.index.tz_localize(None).normalize() if w.index.tz is not None else w.index.normalize()
    dmin, dmax = dates.min(), dates.max()
    periods = dates.to_period(_FREQ[rule])

    labels: dict = {}
    for per in periods.unique():
        lo = max(per.start_time.normalize(), dmin)
        hi = min(per.end_time.normalize(), dmax)
        sess = sessions(lo.date(), hi.date()) if lo <= hi else []
        if sess:
            labels[per] = pd.Timestamp(sess[-1])
    if not labels:
        return w.iloc[0:0].set_axis(out_idx, axis=0)

    lab_row = pd.Series(periods.map(labels.get), index=range(len(w)))
    keep = (lab_row.notna() & (pd.Series(dates, index=range(len(w))) <= lab_row)).to_numpy()
    sub = w.iloc[keep].copy()
    sub_lab = pd.DatetimeIndex(lab_row[keep].to_numpy(), name='date')
    out = sub.set_axis(sub_lab, axis=0).groupby(level=0, sort=True).last()
    out.index.name = 'date'
    return out.ffill() if fill == 'prev' else out
