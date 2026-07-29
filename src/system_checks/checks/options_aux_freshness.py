"""Strategies-tagged check: the BACKTEST's options panel is fresh, and the
OI-derived fields are honest about the provider's missing open interest.

Guards the 2026-07-29 incident class: `options_aggregates_enriched.parquet`
(the only options source `aux_data_loader._day_slice` reads) silently froze at
2026-04-22 when the aggregates collector was retired. `_day_slice` falls back
to "most recent prior date", so every backtest bar since April was served the
April slice with no error — while LIVE read `options_eod.parquet` and stayed
fresh. Rebuild path: `scripts/build_options_aggregates.py` then
`scripts/compute_rolling_options_fields.py`.

Second guard: the current provider feed carries NO open_interest, so gex /
contracts_liquid / iv_centroid_delta / surface_premium must be NULL rather
than a computed 0.0 (a fabricated "no dealer gamma imbalance"). If a future
change reintroduces zeros while OI is absent, this FAILs.
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
            f'every later bar; rebuild via scripts/build_options_aggregates.py '
            f'+ scripts/compute_rolling_options_fields.py')
    return Status.PASS, (
        f'newest {latest.date()} ({lag_days}d), {newest["ticker"].nunique()} '
        f'tickers; OI-derived fields NULL (feed has no open_interest)')
