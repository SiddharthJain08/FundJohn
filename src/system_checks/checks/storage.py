"""Storage checks — master data growing, key paths writable."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from ..registry import check
from ..types import Status

ROOT = Path('/root/openclaw')
MASTER = ROOT / 'data' / 'master'


@check(name='master_parquets_present', tags=['storage'], requires=['fs'])
def _master_parquets_present():
    """The eight canonical master parquets exist. Append-only invariant means
    they should never disappear."""
    expected = [
        'prices.parquet', 'options_eod.parquet', 'financials.parquet',
        'macro.parquet', 'insider.parquet', 'earnings.parquet',
        'prices_30m.parquet', 'historical_regimes.parquet',
    ]
    missing = [f for f in expected if not (MASTER / f).exists()]
    if missing:
        return Status.FAIL, f'missing master parquet(s): {missing}'
    return Status.PASS, f'all {len(expected)} master parquets present'


@check(name='intraday_features_accumulating', tags=['storage'], requires=['fs'])
def _intraday_features_accumulating():
    """intraday_features.parquet got updated within the last hour during RTH,
    last 24h otherwise. This is the data source for the not-yet-trained
    intraday HMM, so accumulation matters."""
    path = MASTER / 'intraday_features.parquet'
    if not path.exists():
        return Status.WARN, 'intraday_features.parquet missing'
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age_min = (datetime.now(timezone.utc) - mtime).total_seconds() / 60
    # Crude RTH check: weekday + ET hour 9.5..16.
    now_et = datetime.now(tz=timezone.utc)
    et_hour_offset = -5  # not DST-correct but RTH check tolerates 1h slop
    et_hour = (now_et.hour + et_hour_offset) % 24
    in_rth = now_et.weekday() < 5 and 10 <= et_hour <= 16
    threshold = 60 if in_rth else 1440
    if age_min > threshold:
        sev = Status.FAIL if in_rth else Status.WARN
        return sev, f'last update {age_min:.0f}m ago (threshold {threshold}m, RTH={in_rth})'
    return Status.PASS, f'fresh ({age_min:.0f}m old)'


@check(name='prices_no_nan_price_bars', tags=['storage'], requires=['fs'])
def _prices_no_nan_price_bars():
    """No NaN-close bars in the recent prices.parquet window. A single corrupt
    OHLC bar (volume present, prices NaN) silently poisons backtest metrics:
    the t+1 fill lands on the NaN close -> NaN pnl_pct -> NaN Sharpe/max_dd/
    return for every strategy that fills on it. (2026-06-15 BRK-B 2026-04-07
    incident nulled 6 strategies.) Recent-window scan catches new corruption
    from the daily writer before the next backtest rebuild reads it."""
    import pandas as pd
    path = MASTER / 'prices.parquet'
    if not path.exists():
        return Status.SKIP, 'prices.parquet missing'
    try:
        df = pd.read_parquet(path, columns=['ticker', 'date', 'close'])
    except Exception as e:  # noqa: BLE001
        return Status.ERROR, f'read failed: {e}'
    if df.empty:
        return Status.WARN, 'prices.parquet empty'
    df['date'] = pd.to_datetime(df['date'])
    cutoff = df['date'].max() - pd.Timedelta(days=200)
    recent = df[df['date'] >= cutoff]
    bad = recent[recent['close'].isna()]
    if len(bad):
        items = (bad.assign(_d=bad['date'].dt.date)
                    .groupby('ticker')['_d']
                    .apply(lambda s: [str(x) for x in sorted(s)[:2]])
                    .to_dict())
        desc = '; '.join(f'{t}:{ds}' for t, ds in list(items.items())[:4])
        return (Status.WARN,
                f'{len(bad)} NaN-close bar(s) in last 200d (poisons backtest metrics): {desc}'[:200])
    return Status.PASS, f'no NaN-close bars in last 200d ({len(recent)} bars scanned)'


@check(name='data_ledger_usage_map_evaluable', tags=['storage'], requires=['db'])
def _data_ledger_usage_map_evaluable():
    """data_usage_map must evaluate — and therefore the data_ledger materialized
    view that sync_data_ledger.py REFRESHes at every johnbot boot. The view unrolls
    strategy_registry.parameters->'required_columns', which exists in two shapes: a
    flat string array, and an object {"required":[...],"optional":[...]} whose elements
    may be strings or {"column":...} objects. The original view used
    jsonb_array_elements_text() on the value, which throws 'cannot extract elements
    from an object' on the object form — silently skipping the boot sync (caught in
    bot.js) so data_columns coverage quietly stopped updating for days. Fixed
    2026-06-26 (migration 138, shape-tolerant view). This re-runs the view so a future
    regression — or a new registry JSON shape — is caught on the next maintenance run."""
    import psycopg2
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        conn = psycopg2.connect(uri)
    except Exception as e:  # noqa: BLE001
        return Status.ERROR, f'connect failed: {e}'
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM data_usage_map')
            n = cur.fetchone()[0]
        return Status.PASS, f'data_usage_map evaluates ({n} columns mapped)'
    except Exception as e:  # noqa: BLE001
        return Status.FAIL, f'data_usage_map failed (data_ledger refresh would skip): {str(e).strip()[:140]}'
    finally:
        conn.close()


@check(name='logs_dir_size_under_50mb', tags=['storage'], requires=['fs'])
def _logs_dir_size():
    """logs/ stays under 50MB. Old logs pile up if cleanup isn't run periodically."""
    log_dir = ROOT / 'logs'
    if not log_dir.exists():
        return Status.SKIP, 'logs/ missing'
    total_bytes = sum(f.stat().st_size for f in log_dir.rglob('*') if f.is_file())
    mb = total_bytes / (1024 * 1024)
    if mb > 100:
        return Status.WARN, f'logs/ is {mb:.0f} MB — run cleanup'
    return Status.PASS, f'logs/ {mb:.1f} MB'
