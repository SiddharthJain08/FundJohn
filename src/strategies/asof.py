"""Point-in-time as-of alignment of a published series onto a bar index.

Same-day contract: a value is usable on bar ``t`` only if it was published
strictly BEFORE ``t`` (``same_day='after_close'``, the default). An
after-close publication (earnings, a macro print) is therefore NOT usable on
its own bar. ``same_day='same_bar'`` allows ``available_at == t`` for series
known to be published before the bar's decision time.

Imports only pandas.
"""
from __future__ import annotations

import pandas as pd

_POS = '_asof_pos'
_DKEY = '_asof_date'


def asof_align(right: pd.DataFrame, left_index: pd.Index, *,
               on: str = 'available_at', by: str | None = None,
               tolerance: pd.Timedelta | None = None,
               same_day: str = 'after_close') -> pd.DataFrame:
    """Backward as-of join of ``right`` (keyed on column ``on``) onto ``left_index``.

    ``by is None``: ``left_index`` is a DatetimeIndex. ``by`` given:
    ``left_index`` is a 2-level MultiIndex ``(by, date)`` and ``right`` must
    carry a ``by`` column. The result is indexed by ``left_index`` (order,
    duplicates and names kept) with all of ``right``'s columns; unmatched
    rows are all-NaN.
    """
    if same_day == 'after_close':
        exact = False
    elif same_day == 'same_bar':
        exact = True
    else:
        raise ValueError(
            "same_day must be 'after_close' or 'same_bar', got %r" % (same_day,))
    if on not in right.columns:
        raise KeyError(on)
    if by is not None and by not in right.columns:
        raise KeyError(by)

    if by is None:
        ldates = pd.DatetimeIndex(left_index)
        lkeys = None
    else:
        ldates = pd.DatetimeIndex(left_index.get_level_values(1))
        lkeys = left_index.get_level_values(0)

    rkey = pd.to_datetime(right[on])
    if (rkey.dt.tz is None) != (ldates.tz is None):
        raise TypeError(
            'asof_align tz mismatch: right[%r] dtype=%s vs left_index dtype=%s'
            % (on, rkey.dtype, ldates.dtype))

    left = pd.DataFrame({_DKEY: ldates.as_unit('ns'), _POS: range(len(ldates))})
    if by is not None:
        left[by] = lkeys
    left = left[left[_DKEY].notna()]
    r = right.copy()
    r[_DKEY] = rkey.dt.as_unit('ns')
    r = r[r[_DKEY].notna()]
    left = left.sort_values(_DKEY, kind='mergesort')
    r = r.sort_values(_DKEY, kind='mergesort')

    merged = pd.merge_asof(left, r, on=_DKEY, by=by, direction='backward',
                           tolerance=tolerance, allow_exact_matches=exact)
    merged = merged.sort_values(_POS, kind='mergesort')
    cols = list(right.columns)
    out = merged.set_index(_POS)[cols].reindex(range(len(ldates)))
    out.index = left_index
    return out
