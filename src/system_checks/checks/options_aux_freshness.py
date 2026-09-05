"""Strategies-tagged check: the BACKTEST's options panel is fresh, and the
OI-derived fields are honest about the provider's missing open interest.

Guards the 2026-07-29 incident class: `options_aggregates_enriched.parquet`
(the only options source `aux_data_loader._day_slice` reads) silently froze at
2026-04-22 when the aggregates collector was retired. `_day_slice` falls back
to "most recent prior date", so every backtest bar since April was served the
April slice with no error — while LIVE read `options_eod.parquet` and stayed
fresh. Rebuild path (since the 2026-09-04 surface v2 build):
`scripts/build_options_surface.py` then `scripts/compute_rolling_options_fields.py`.

Second guard: OI-derived fields (gex / contracts_liquid / iv_centroid_delta /
surface_premium) must be NULL when the session behind them carries no open
interest, never a computed 0.0 (a fabricated "no dealer gamma imbalance").
If a future change reintroduces zeros while OI is absent, this FAILs.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..registry import check
from ..types import Status

_PANEL = Path(os.environ.get(
    'OPTIONS_ENRICHED_PANEL',
    '/root/openclaw/data/master/options_aggregates_enriched.parquet'))
# Options collection is EOD + weekday-only; allow a long weekend + a holiday.
_MAX_LAG_DAYS = int(os.environ.get('OPTIONS_AUX_MAX_LAG_DAYS', '5'))
_OI_FIELDS = ('gex', 'contracts_liquid', 'iv_centroid_delta', 'surface_premium')


@check(name='options_aux_freshness', tags=['strategies'], requires=[])
def _options_aux_freshness():
    if not _PANEL.exists():
        return Status.WARN, f'enriched options panel missing: {_PANEL}'
    try:
        import pandas as pd
        cols = ['date', 'ticker', *(_OI_FIELDS)]
        df = pd.read_parquet(_PANEL, columns=cols)
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'panel unreadable: {e}'
    if df.empty:
        return Status.FAIL, 'enriched options panel is empty'

    import pandas as pd
    latest = pd.to_datetime(df['date']).max()
    lag_days = (pd.Timestamp.today().normalize() - latest.normalize()).days
    newest = df[pd.to_datetime(df['date']) == latest]

    # A zero (not null) in an OI-derived field while the feed carries no OI is
    # the fabricated-fact regression this check exists to catch.
    fabricated = [f for f in _OI_FIELDS
                  if f in newest.columns
                  and newest[f].notna().any()
                  and (newest[f].fillna(0) == 0).all()]
    if fabricated:
        return Status.FAIL, (
            f'OI-derived field(s) {fabricated} are all-zero-non-null on '
            f'{latest.date()} — open_interest is absent from the feed, so these '
            f'must be NULL, not 0.0 (fabricated "no imbalance")')

    if lag_days > _MAX_LAG_DAYS:
        return Status.FAIL, (
            f'options panel stale {lag_days}d (max {_MAX_LAG_DAYS}d, newest '
            f'{latest.date()}) — backtests are silently reading that slice for '
            f'every later bar; rebuild via scripts/build_options_surface.py '
            f'+ scripts/compute_rolling_options_fields.py')
    return Status.PASS, (
        f'newest {latest.date()} ({lag_days}d), {newest["ticker"].nunique()} '
        f'tickers; OI-derived fields honest (NULL when no CBOE session, '
        f'never a fabricated 0)')


_OI_MIN_TICKERS = int(os.environ.get('OPTIONS_OI_MIN_TICKERS', '400'))


def oi_coverage(min_tickers: int = _OI_MIN_TICKERS):
    """FAIL when the latest panel date carries CBOE open interest for fewer than
    `min_tickers` tickers while a CBOE session for the prior day exists
    (spec 2026-09-04 B.4). PASS when OI is present; WARN when no CBOE session
    is available yet (the stream started 2026-08-21).

    On the latest real session (2026-09-03-ish, 11 CBOE sessions landed since
    2026-08-21) ~553 tickers carry non-null pcr_oi — the `min_tickers=400`
    default documents that headroom rather than pinning the exact count.
    """
    import pandas as pd
    from strategies.options_oi import cboe_session_for
    panel = Path(os.environ.get('OPTIONS_ENRICHED_PANEL', str(_PANEL)))
    try:
        df = pd.read_parquet(panel, columns=['ticker', 'date', 'pcr_oi'])
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'panel unreadable: {e}'
    if df.empty:
        return Status.FAIL, 'enriched options panel is empty'
    latest = pd.to_datetime(df['date']).max()
    if cboe_session_for(latest) is None:
        return Status.WARN, f'no CBOE session before {latest.date()} — OI features legitimately NULL'
    n = int(df[pd.to_datetime(df['date']) == latest]['pcr_oi'].notna().sum())
    if n < min_tickers:
        return Status.FAIL, f'only {n} tickers carry CBOE open interest on {latest.date()} (need ≥ {min_tickers})'
    return Status.PASS, f'{n} tickers carry CBOE open interest on {latest.date()}'


@check(name='options_oi_coverage', tags=['strategies', 'storage'], requires=[])
def _check_oi_coverage():
    return oi_coverage()
