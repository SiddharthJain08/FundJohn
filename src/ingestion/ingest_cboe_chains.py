"""CBOE delayed option chains → data/master/cboe_chains/date=YYYY-MM-DD.parquet
                              + data/master/cboe_chain_aggregates.parquet

Why (2026-08-23 provider audit). Alpaca's option snapshots carry NO open
interest; options_eod.parquet only starts 2026-04-08 and its greeks are valid
from 06-29; the engine's gex / pcr_oi / unusual-flow features degrade without
OI (engine.py:1201-1206). CBOE publishes a delayed (15-min) chain per
underlying as plain JSON with no key — IV, all five greeks, theo, OI, volume,
bid/ask, and the underlying's iv30 — at cdn.cboe.com. AAPL: 3,348 contracts,
1.5 MB, ~130 ms. Capturing it once per session after the close builds our own
OI / IV history forward at zero cost. (Backfill is impossible from this
source — it is a snapshot — which is exactly why the capture must not skip
days.)

Layout
  data/master/cboe_chains/date=<session>.parquet   one file per session,
      every contract, written once (streamed in row-group batches through a
      tmp file + os.replace; never an append to a multi-GB master — the
      2026-08 options_eod flush SIGKILL was that anti-pattern).
  data/master/cboe_chain_aggregates.parquet        one row per
      (date, underlying): iv30, OI/volume totals, put/call ratios, a dealer
      gamma proxy, ATM IV and nearest expiry. Small; append_dedup(replace).

Append-only (CLAUDE.md): partitions are only ever added; the aggregates
master only gains rows/columns.

Session date: CBOE's `timestamp` is the feed time, not the session — Friday's
chain carries a Saturday/Sunday stamp. session_date_for() rolls weekend and
pre-open stamps back to the last session; the 17:00 ET weekday timer makes
it unambiguous. `--date` overrides.

Universe: `--symbols`, else universe_config (active AND has_options — the
551 names the collector's options phase used) plus INDEX_SYMBOLS (CBOE
prefixes indices with `_`).

Counters are the contract ("read the COUNTERS, not the exit code"):
  rc=1  nothing could be fetched at all, or the partition write failed
  rc=0  otherwise, with tickers_ok/tickers_failed/contracts/aggregates printed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

CBOE_BASE = 'https://cdn.cboe.com/api/global/delayed_quotes/options/'
PARTITION_ROOT = ROOT / 'data' / 'master' / 'cboe_chains'
AGGREGATES_PATH = ROOT / 'data' / 'master' / 'cboe_chain_aggregates.parquet'
AGGREGATE_KEYS = ['date', 'underlying']
INDEX_SYMBOLS = ['_SPX', '_VIX', '_NDX', '_RUT', '_DJX']
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
_TIMEOUT = 30
_OCC_RE = re.compile(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$')

CONTRACT_SCHEMA = pa.schema([
    ('date', pa.date32()),
    ('underlying', pa.string()),
    ('contract_symbol', pa.string()),
    ('root', pa.string()),
    ('expiry', pa.date32()),
    ('option_type', pa.string()),
    ('strike', pa.float64()),
    ('bid', pa.float64()), ('bid_size', pa.float64()),
    ('ask', pa.float64()), ('ask_size', pa.float64()),
    ('iv', pa.float64()),
    ('open_interest', pa.float64()),
    ('volume', pa.float64()),
    ('delta', pa.float64()), ('gamma', pa.float64()), ('vega', pa.float64()),
    ('theta', pa.float64()), ('rho', pa.float64()),
    ('theo', pa.float64()),
    ('last_trade_price', pa.float64()),
    ('last_trade_time', pa.string()),
    ('prev_day_close', pa.float64()),
    ('underlying_price', pa.float64()),
    ('feed_timestamp', pa.string()),
    ('source', pa.string()),
])


# ── parsing ──────────────────────────────────────────────────────────────────

def parse_occ_symbol(sym: str):
    """OCC option symbol → (root, expiry, 'C'|'P', strike). None if malformed."""
    m = _OCC_RE.match(str(sym or ''))
    if not m:
        return None
    root, ymd, cp, strike8 = m.groups()
    try:
        expiry = dt.datetime.strptime(ymd, '%y%m%d').date()
    except ValueError:
        return None
    return root, expiry, cp, int(strike8) / 1000.0


def _prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def session_date_for(timestamp: str) -> dt.date:
    """Last completed session for a CBOE feed timestamp ('YYYY-MM-DD HH:MM:SS').
    Weekend stamps → Friday; weekday stamps before 09:30 → previous session."""
    ts = dt.datetime.strptime(str(timestamp)[:19], '%Y-%m-%d %H:%M:%S')
    d = ts.date()
    if d.weekday() >= 5:
        return _prev_business_day(d)
    if ts.time() < dt.time(9, 30):
        return _prev_business_day(d)
    return d


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def chain_rows(payload: dict, underlying: str, session: dt.date) -> list[dict]:
    data = payload.get('data') or {}
    spot = _f(data.get('current_price'))
    feed_ts = str(payload.get('timestamp') or '')
    rows = []
    for o in data.get('options') or []:
        parsed = parse_occ_symbol(o.get('option'))
        if not parsed:
            continue
        root, expiry, cp, strike = parsed
        rows.append({
            'date': session, 'underlying': underlying.lstrip('_'), 'contract_symbol': o['option'],
            'root': root, 'expiry': expiry, 'option_type': cp, 'strike': strike,
            'bid': _f(o.get('bid')), 'bid_size': _f(o.get('bid_size')),
            'ask': _f(o.get('ask')), 'ask_size': _f(o.get('ask_size')),
            'iv': _f(o.get('iv')), 'open_interest': _f(o.get('open_interest')),
            'volume': _f(o.get('volume')),
            'delta': _f(o.get('delta')), 'gamma': _f(o.get('gamma')), 'vega': _f(o.get('vega')),
            'theta': _f(o.get('theta')), 'rho': _f(o.get('rho')), 'theo': _f(o.get('theo')),
            'last_trade_price': _f(o.get('last_trade_price')),
            'last_trade_time': o.get('last_trade_time'),
            'prev_day_close': _f(o.get('prev_day_close')),
            'underlying_price': spot, 'feed_timestamp': feed_ts, 'source': 'cboe',
        })
    return rows


def chain_aggregates(rows: list[dict], payload: dict, underlying: str, session: dt.date) -> dict:
    data = payload.get('data') or {}
    spot = _f(data.get('current_price'))
    agg = {
        'date': session, 'underlying': underlying.lstrip('_'), 'underlying_price': spot,
        'iv30': _f(data.get('iv30')), 'iv30_change': _f(data.get('iv30_change')),
        'n_contracts': len(rows), 'n_expiries': 0,
        'call_oi': 0.0, 'put_oi': 0.0, 'call_volume': 0.0, 'put_volume': 0.0,
        'pcr_oi': None, 'pcr_volume': None, 'gex': None, 'atm_iv': None, 'nearest_expiry': None,
        'feed_timestamp': str(payload.get('timestamp') or ''), 'source': 'cboe',
    }
    if not rows:
        return agg
    df = pd.DataFrame(rows)
    calls, puts = df[df.option_type == 'C'], df[df.option_type == 'P']
    agg['call_oi'] = float(calls.open_interest.fillna(0).sum())
    agg['put_oi'] = float(puts.open_interest.fillna(0).sum())
    agg['call_volume'] = float(calls.volume.fillna(0).sum())
    agg['put_volume'] = float(puts.volume.fillna(0).sum())
    agg['n_expiries'] = int(df.expiry.nunique())
    if agg['call_oi'] > 0:
        agg['pcr_oi'] = agg['put_oi'] / agg['call_oi']
    if agg['call_volume'] > 0:
        agg['pcr_volume'] = agg['put_volume'] / agg['call_volume']
    if spot:
        sign = df.option_type.map({'C': 1.0, 'P': -1.0})
        agg['gex'] = float((df.gamma.fillna(0) * df.open_interest.fillna(0) * sign).sum() * 100 * spot ** 2 * 0.01)
        # ATM IV at the nearest expiry with ≥ 7 DTE (skip the dying weekly)
        dte = (pd.to_datetime(df.expiry) - pd.Timestamp(session)).dt.days
        cand = df[dte >= 7]
        if cand.empty:
            cand = df
        near = cand.expiry.min()
        agg['nearest_expiry'] = near
        slab = cand[cand.expiry == near]
        if not slab.empty:
            k = slab.iloc[(slab.strike - spot).abs().argsort()[:1]].strike.iloc[0]
            atm = slab[(slab.strike == k) & slab.iv.notna()]
            if not atm.empty:
                agg['atm_iv'] = float(atm.iv.mean())
    return agg


# ── I/O ──────────────────────────────────────────────────────────────────────

def cboe_url(symbol: str) -> str:
    return f'{CBOE_BASE}{symbol}.json'


def fetch_chain(symbol: str) -> dict:
    import requests
    r = requests.get(cboe_url(symbol), timeout=_TIMEOUT, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    r.raise_for_status()
    return r.json()


def _retry_after_s(exc) -> float | None:
    resp = getattr(exc, 'response', None)
    try:
        ra = (getattr(resp, 'headers', None) or {}).get('Retry-After')
        return float(ra) if ra is not None else None
    except (TypeError, ValueError):
        return None


def fetch_with_retry(symbol: str, attempts: int = 4, sleep=time.sleep) -> dict:
    """fetch_chain with 429/5xx backoff. cdn.cboe.com rate-limits bursts
    (first run 2026-08-23: 494 of 556 symbols → 429 at 4 workers / 50 ms);
    honour Retry-After, else exponential 1,2,4… s."""
    last = None
    for i in range(max(1, attempts)):
        try:
            return fetch_chain(symbol)
        except Exception as exc:  # noqa: BLE001
            last = exc
            msg = str(exc)
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            retryable = status in (429, 500, 502, 503, 504) or any(c in msg for c in ('429', '503', '502', 'timed out', 'Timeout'))
            if not retryable or i == attempts - 1:
                break
            wait = _retry_after_s(exc) or float(2 ** i)
            sleep(min(wait, 60.0))
    raise last


def partition_path(session: dt.date, root: Path = None) -> Path:
    return Path(root or PARTITION_ROOT) / f'date={session.isoformat()}.parquet'


def partition_exists(session: dt.date, root: Path = None) -> bool:
    return partition_path(session, root).exists()


class PartitionWriter:
    """Streams contract rows into one session partition via a tmp file and a
    final os.replace — readers never see a partial file, and memory stays at
    one batch regardless of how many underlyings are captured."""

    def __init__(self, path: Path, batch_rows: int = 50_000):
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + '.tmp')
        self.batch_rows = batch_rows
        self._buf: list[dict] = []
        self._writer = None
        self.rows = 0

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(self.tmp, CONTRACT_SCHEMA, compression='zstd')
        return self

    def write_rows(self, rows: list[dict]) -> None:
        self._buf.extend(rows)
        if len(self._buf) >= self.batch_rows:
            self._flush()

    def _flush(self):
        if not self._buf:
            return
        table = pa.Table.from_pylist(self._buf, schema=CONTRACT_SCHEMA)
        self._writer.write_table(table)
        self.rows += len(self._buf)
        self._buf = []

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._flush()
            self._writer.close()
            if exc_type is None:
                os.replace(self.tmp, self.path)
            else:
                self.tmp.unlink(missing_ok=True)
        finally:
            self._writer = None
        return False


def write_aggregates(aggs: list[dict], master_path: Path = None) -> int:
    from src.data.parquet_store import append_dedup
    if not aggs:
        return 0
    df = pd.DataFrame(aggs)
    return append_dedup(master_path or AGGREGATES_PATH, df, AGGREGATE_KEYS, mode='replace')


def load_universe() -> list[str]:
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
        cur.execute('SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE AND has_options = TRUE ORDER BY ticker')
        return [r[0] for r in cur.fetchall()]


# ── orchestration ────────────────────────────────────────────────────────────

def run(symbols: list[str], session: dt.date | None = None, workers: int = 4,
        force: bool = False, pace_s: float = 0.0) -> dict:
    stats = {'session': None, 'symbols': len(symbols), 'tickers_ok': 0, 'tickers_failed': 0,
             'contracts': 0, 'aggregates': 0, 'skipped_existing': False, 'failed': []}
    t0 = time.monotonic()

    # Resolve the session from the first successful fetch when not given.
    fetched: dict[str, dict] = {}
    if session is None:
        for s in symbols[:5]:
            try:
                fetched[s] = fetch_chain(s)
                session = session_date_for(fetched[s]['timestamp'])
                break
            except Exception as exc:  # noqa: BLE001
                log.warning('cboe: probe %s failed: %s', s, exc)
        if session is None:
            stats['error'] = 'no chain could be fetched to resolve the session date'
            return stats
    stats['session'] = session.isoformat()

    out = partition_path(session)
    if out.exists() and not force:
        stats['skipped_existing'] = True
        log.info('cboe: partition %s exists — skipping (use --force to rewrite)', out.name)
        return stats

    aggs: list[dict] = []
    with PartitionWriter(out) as w:
        def _one(sym):
            if sym in fetched:
                return sym, fetched[sym]
            if pace_s:
                time.sleep(pace_s)
            return sym, fetch_with_retry(sym)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(_one, s) for s in symbols]
            for fut in as_completed(futs):
                try:
                    sym, payload = fut.result()
                except Exception as exc:  # noqa: BLE001
                    stats['tickers_failed'] += 1
                    stats['failed'].append(str(exc)[:80])
                    continue
                rows = chain_rows(payload, sym, session)
                w.write_rows(rows)
                aggs.append(chain_aggregates(rows, payload, sym, session))
                stats['tickers_ok'] += 1
                stats['contracts'] += len(rows)
    stats['aggregates'] = len(aggs)
    if aggs:
        write_aggregates(aggs)
    stats['elapsed_s'] = round(time.monotonic() - t0, 1)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--symbols', nargs='*', help='override universe (CBOE index symbols start with _)')
    ap.add_argument('--date', help='session date YYYY-MM-DD (default: inferred from the feed timestamp)')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--pace', type=float, default=0.4, help='seconds between requests per worker')
    ap.add_argument('--force', action='store_true', help='rewrite an existing partition')
    ap.add_argument('--no-index', action='store_true', help='skip the _SPX/_VIX/… index chains')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if args.symbols:
        symbols = args.symbols
    else:
        symbols = load_universe()
        if not args.no_index:
            symbols = INDEX_SYMBOLS + symbols
    session = dt.date.fromisoformat(args.date) if args.date else None
    stats = run(symbols, session=session, workers=args.workers, force=args.force, pace_s=args.pace)
    failed_preview = '; '.join(stats.pop('failed', [])[:3])
    print('[cboe-chains] ' + ' '.join(f'{k}={v}' for k, v in stats.items()) + (f' failed_preview="{failed_preview}"' if failed_preview else ''))
    if stats.get('error') or (stats['tickers_ok'] == 0 and not stats['skipped_existing']):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
