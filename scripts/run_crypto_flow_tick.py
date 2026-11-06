#!/usr/bin/env python3
"""15-minute crypto flow tick: run due providers → whale-event archive → per-ticker + market
features → crypto_flows_1h.parquet + crypto_flow_snapshot + provider health.

Gated on OPENCLAW_CRYPTO_FLOWS=1 (scheduler job `crypto-flow-tick`, cron-schedule.js). Skips (--force
overrides the skips, never the gate): the 15:00–15:50 ET weekday compute window, a held daily-cycle Redis run
lock (src/lib/run_lock.py — production compute is the LangGraph cycle, which never runs the orchestrator), any
running compute step process, and MemAvailable below OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB (default 1500).
Providers run strictly sequentially (they share one state file) under a tick deadline
(OPENCLAW_CRYPTO_FLOW_DEADLINE_S, default 600 s, cut short at the next weekday 15:00 ET); loop providers get the
same bound as `deadline_at` (a time.monotonic() value) and return partial results. The scheduler wraps the run
in `timeout 840`; SIGTERM releases the overlap lock and prints the summary line with skipped=sigterm.

Hourly/daily provider fields are carried between ticks from the state file's RAW `slow_obs`
({ticker: {field: {v, ts, cad}}}), each expiring after 3 cadences — never from the computed snapshot features.

--dry-run: no parquet / DB writes, Santiment not run, real state file untouched (providers get a
throw-away copy, so on-chain cursors do not advance), DB not contacted.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.ingestion.crypto_flow import features as F  # noqa: E402
from src.ingestion.crypto_flow import store  # noqa: E402
from src.ingestion.crypto_flow.models import MARKET_TICKER, ProviderResult  # noqa: E402
from src.ingestion.crypto_flow.providers.base import run_provider  # noqa: E402
from src.lib import run_lock  # noqa: E402

STATE_PATH = ROOT / 'data/master/.crypto_flow_state.json'
TICK_DEADLINE_S = 600
_ET = ZoneInfo('America/New_York')
_MIN_GAP = {'hourly': timedelta(minutes=55), 'daily': timedelta(hours=23)}
_DAILY_RETRY = timedelta(hours=1)  # a failed daily provider is retried no sooner than this
_SLOW_MAX_AGE = {'hourly': timedelta(hours=3), 'daily': timedelta(days=3)}  # 3 x cadence
_SNAPSHOT_HINT_MAX_AGE = timedelta(hours=2)
_SPOT_HINT_MAX_AGE = timedelta(hours=3)
_NOT_ATTEMPTED = ('deadline', 'skipped_dry_run')
_STR_COLS = {'oi_venues', 'oi_price_divergence', 'whale_direction', 'whale_direction_btc', 'cot_report_date',
             'providers_failed_this_tick', 'stable_supply_missing'}
_BOOL_COLS = {'delev_1h', 'delev_market', 'options_degraded_btc', 'oi_usd_total_venues_mixed'}
# any of these in a process command line = a compute step is running (LangGraph spawns the step scripts directly)
COMPUTE_PATTERNS = ['engine.py', 'regime_blended_sizer_live.py', 'alpaca_executor', 'run_collector_once',
                    'pipeline_orchestrator.py']
MIN_MEM_MB_DEFAULT = 1500
TICK_PROVIDER = '__tick__'  # provider_health row for the tick itself (locked / completed)
_SNAPSHOT_ONLY = {'shock_lines', 'whale_score_components'}  # kept in the snapshot JSON, not the parquet


def _pgrep(pattern: str) -> bool:
    return subprocess.run(['pgrep', '-f', pattern], capture_output=True).returncode == 0


def redis_run_lock_held(now_utc: datetime) -> bool:
    """GET the shared daily-cycle run lock for today's ET and UTC dates (cron-schedule dispatches the LangGraph
    cycle with the UTC date; the orchestrator with --date). Any value → held; any error → False (fail-open: the
    clock window, the process check and the memory floor still apply)."""
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                           socket_connect_timeout=2, socket_timeout=2)
        dates = {now_utc.astimezone(_ET).date().isoformat(), now_utc.astimezone(timezone.utc).date().isoformat()}
        return any(r.get(run_lock.lock_key(d)) is not None for d in sorted(dates))
    except Exception:  # noqa: BLE001
        return False


def mem_available_mb(path: str = '/proc/meminfo') -> float | None:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def should_skip(now_utc: datetime, *, pgrep=_pgrep, lock_held=None, mem_mb=mem_available_mb,
                force: bool = False) -> str | None:
    """'compute_window' | 'run_lock_held' | 'compute_running' | 'low_memory' | None. --force bypasses all four."""
    if force:
        return None
    et = now_utc.astimezone(_ET)
    if et.weekday() < 5 and 15 * 60 <= et.hour * 60 + et.minute < 15 * 60 + 50:
        return 'compute_window'
    if (lock_held or (lambda: redis_run_lock_held(now_utc)))():
        return 'run_lock_held'
    if any(pgrep(p) for p in COMPUTE_PATTERNS):
        return 'compute_running'
    floor = float(os.environ.get('OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB') or MIN_MEM_MB_DEFAULT)
    avail = mem_mb()
    if avail is not None and avail < floor:
        return 'low_memory'
    return None


def tick_deadline(now_utc: datetime, deadline_s: float) -> datetime:
    """min(now + deadline_s, the next weekday 15:00 ET) — a tick never runs into the compute window."""
    et = now_utc.astimezone(_ET)
    d = et.date()
    while True:
        cut = datetime.combine(d, datetime.min.time().replace(hour=15), tzinfo=_ET)
        if d.weekday() < 5 and cut > et:
            break
        d += timedelta(days=1)
    return min(now_utc + timedelta(seconds=deadline_s), cut.astimezone(timezone.utc))


def due_providers(providers: list, now_utc: datetime, state: dict) -> list:
    """'tick' always; 'hourly' when never run or ≥ 55 min ago; 'daily' when ≥ 23 h ago — except a daily provider
    whose last attempt failed (last_attempt newer than last_run) waits 1 h after that attempt."""
    last = (state or {}).get('last_run') or {}
    attempts = (state or {}).get('last_attempt') or {}
    out = []
    for p in providers:
        gap, prev, att = _MIN_GAP.get(p.cadence), last.get(p.name), attempts.get(p.name)
        try:
            if p.cadence == 'daily' and att:
                a = datetime.fromisoformat(att)
                if (not prev or a > datetime.fromisoformat(prev)) and now_utc - a < _DAILY_RETRY:
                    continue
            if gap is None or not prev or now_utc - datetime.fromisoformat(prev) >= gap:
                out.append(p)
        except (TypeError, ValueError):
            out.append(p)
    return out


def _slow_fresh(entry, now_utc: datetime) -> bool:
    """A slow_obs entry is usable for 3 cadences (hourly → 3 h, daily → 3 d; unknown cadence → hourly)."""
    try:
        ts = datetime.fromisoformat(entry['ts'])
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return now_utc - ts <= _SLOW_MAX_AGE.get(entry.get('cad'), _SLOW_MAX_AGE['hourly'])
    except (TypeError, KeyError, ValueError, AttributeError):
        return False


def prune_slow_obs(slow_obs: dict, now_utc: datetime) -> dict:
    out = {}
    for tk, fields in (slow_obs or {}).items():
        keep = {k: e for k, e in (fields or {}).items() if isinstance(e, dict) and _slow_fresh(e, now_utc)}
        if keep:
            out[tk] = keep
    return out


def update_slow_obs(slow_obs: dict, results: list, cadence_of: dict, now_utc: datetime) -> dict:
    """Pruned `slow_obs` overlaid with every RAW field this tick's hourly/daily providers observed.
    Not-due / failed slow providers keep their entries until expiry; tick-cadence fields never enter."""
    out = prune_slow_obs(slow_obs, now_utc)
    ts = now_utc.isoformat()
    for r in results:
        cad = cadence_of.get(r.provider)
        if cad not in _SLOW_MAX_AGE:
            continue
        for o in r.observations:
            row = out.setdefault(o.ticker, {})
            for k, v in o.fields.items():
                row[k] = {'v': store._sanitize(v), 'ts': ts, 'cad': cad}
    return out


def merge_observations(slow_obs: dict, observations: list, *, now_utc: datetime) -> dict[str, dict]:
    """Per ticker: the carried RAW hourly/daily-provider fields (`slow_obs`, unexpired only), overlaid by this
    tick's observation fields in provider order (later providers win on the same key). Never seeded from the
    previous snapshot's computed features: outputs such as price_usd / ret_* are transforms, not observations."""
    merged = {tk: {k: e['v'] for k, e in fields.items()} for tk, fields in prune_slow_obs(slow_obs, now_utc).items()}
    for o in observations:
        merged.setdefault(o.ticker, {}).update(o.fields)
    return merged


def price_hints(prev: dict, slow_obs: dict, now_utc: datetime) -> dict[str, float]:
    """ticker -> USD for on-chain providers: the previous snapshot's price_usd when that row is < 2 h old, else the
    carried macro_sent spot (`spot_px`) when < 3 h old, else nothing (the provider fetches its own)."""
    out = {}
    for tk in set(prev or {}) | set(slow_obs or {}):
        f = (prev or {}).get(tk) or {}
        ts = f.get('_ts_utc')
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = None
        if isinstance(ts, datetime):
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            v = F._f(f.get('price_usd'))
            if v and now_utc - ts < _SNAPSHOT_HINT_MAX_AGE:
                out[tk] = v
                continue
        e = ((slow_obs or {}).get(tk) or {}).get('spot_px')
        if isinstance(e, dict):
            try:
                ts = datetime.fromisoformat(e['ts'])
                ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            except (TypeError, KeyError, ValueError):
                continue
            v = F._f(e.get('v'))
            if v and now_utc - ts < _SPOT_HINT_MAX_AGE:
                out[tk] = v
    return out


def build_flows_frame(per_ticker: dict[str, dict], market: dict, now_utc: datetime) -> pd.DataFrame:
    """One row per ticker + __MARKET__, ts_utc floored to the hour, stable dtypes for append_dedup merges."""
    mkt = {k: v for k, v in market.items() if k not in _SNAPSHOT_ONLY}
    mkt['providers_failed_this_tick'] = ','.join(market.get('providers_failed_this_tick') or [])
    cols = [c for c in F.FEATURE_COLS if c not in _SNAPSHOT_ONLY] + [k for k in mkt if k not in F.FEATURE_COLS]
    rows = [{'ticker': tk, **{c: f.get(c) for c in cols}} for tk, f in per_ticker.items()]
    rows.append({'ticker': MARKET_TICKER, **{c: mkt.get(c) for c in cols}})
    df = pd.DataFrame(rows, columns=['ticker', *cols])
    for c in cols:
        if c in _STR_COLS:
            df[c] = df[c].map(lambda v: None if v is None or v != v else str(v)).astype('string')
        elif c in _BOOL_COLS:
            df[c] = df[c].astype('boolean')
        else:
            df[c] = pd.to_numeric(df[c], errors='coerce').astype('float64')
    df.insert(1, 'ts_utc', pd.Timestamp(now_utc).tz_convert('UTC').floor('h'))
    df['ts_utc'] = df['ts_utc'].astype('datetime64[ns, UTC]')
    return df


def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, last_run: dict, last_attempt: dict, slow_obs: dict) -> None:
    """Re-read the file (providers wrote their cursors into it during the tick) and merge only our keys."""
    state = _load_json(path)
    state['last_run'] = {**(state.get('last_run') or {}), **last_run}
    state['last_attempt'] = {**(state.get('last_attempt') or {}), **last_attempt}
    state['slow_obs'] = slow_obs
    state.pop('slow_keys', None)  # pre-slow_obs carry list (snapshot-feature carry, retired)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _run_providers(due, *, assets, since, now_utc, hints, work_state, onchain_daily_path, dry_run,
                   deadline_s, clock, t0, deadline_at=None):
    results = []
    for p in due:
        if dry_run and p.name == 'santiment':  # writes its own parquet inside _fetch
            results.append(ProviderResult(p.name, True, [], [], 0, 0, skipped='skipped_dry_run'))
            continue
        if clock() - t0 > deadline_s:
            results.append(ProviderResult(p.name, True, [], [], 0, 0, skipped='deadline'))
            continue
        if hasattr(p, 'state_path'):
            p.state_path = work_state
        if hasattr(p, 'onchain_daily_path'):
            p.onchain_daily_path = onchain_daily_path
        if hasattr(p, 'price_hints'):
            p.price_hints = dict(hints)
        if hasattr(p, 'price_hint'):
            p.price_hint = hints.get('BTC-USD')
        if hasattr(p, 'deadline_at'):
            p.deadline_at = deadline_at
        results.append(run_provider(p, assets, since, now_utc))
    return results


def _summary(c: dict) -> None:
    print(f"[crypto-flow] ts={c['ts']} providers_ok={c['providers_ok']} providers_failed={c['providers_failed']} "
          f"providers_skipped={c['providers_skipped']} events_new={c['events_new']} flows_rows={c['flows_rows']} "
          f"snapshot_rows={c['snapshot_rows']} degraded={','.join(c['degraded']) or '-'} "
          f"skipped={','.join(c['skipped']) or '-'} dt_s={c['dt_s']}")


def _empty_counters(now_utc: datetime, skipped: str, dt_s: float = 0.0) -> dict:
    return {'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'), 'providers_ok': 0, 'providers_failed': 0,
            'providers_skipped': 0, 'events_new': 0, 'flows_rows': 0, 'snapshot_rows': 0, 'degraded': [],
            'skipped': [skipped], 'skipped_reasons': {}, 'dt_s': dt_s}


class TickTerminated(BaseException):
    """Raised by the SIGTERM handler. BaseException so run_provider's `except Exception` cannot swallow it."""


def make_sigterm_handler(fh, now_utc: datetime, t0: float):
    """SIGTERM (scheduler `timeout 840`): release the overlap flock, print the summary line, abort the tick."""
    def handler(signum, frame):
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        print('[crypto-flow] terminated by deadline')
        _summary(_empty_counters(now_utc, 'sigterm', round(time.monotonic() - t0, 1)))
        sys.stdout.flush()
        raise TickTerminated()
    return handler


def _write_locked_health(conn, now_utc: datetime, dry_run: bool) -> None:
    """A skipped=locked tick is a provider_health row, so a hung tick shows on /api/crypto/providers in 15 min."""
    if conn is None or dry_run:
        return
    try:
        store.write_provider_health(conn, now_utc, [ProviderResult(TICK_PROVIDER, False, [], [], 0, 0,
                                                                   err='previous tick still holds the lock',
                                                                   skipped='locked')])
    except Exception as e:  # noqa: BLE001
        print(f'[crypto-flow] locked health row failed: {type(e).__name__}: {e}')


def run_tick(*, now_utc: datetime, state_path: Path, install_sigterm: bool = False, **kw) -> dict:
    """Overlap guard: an exclusive non-blocking flock on <state_path>.lock; a held lock skips the tick (and writes
    a `__tick__` provider_health row). install_sigterm (main only): SIGTERM releases the lock + prints the summary."""
    lock = Path(str(state_path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, 'a') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[crypto-flow] skipped=locked')
            c = _empty_counters(now_utc, 'locked')
            _summary(c)
            _write_locked_health(kw.get('conn'), now_utc, kw.get('dry_run', False))
            return c
        prev_handler = None
        if install_sigterm:
            prev_handler = signal.signal(signal.SIGTERM, make_sigterm_handler(fh, now_utc, time.monotonic()))
        try:
            return _tick(now_utc=now_utc, state_path=state_path, **kw)
        finally:
            if install_sigterm:
                signal.signal(signal.SIGTERM, prev_handler if prev_handler is not None else signal.SIG_DFL)
            fcntl.flock(fh, fcntl.LOCK_UN)


def _tick(*, now_utc: datetime, assets, providers, conn, state_path: Path, dry_run: bool,
          since: datetime | None = None, deadline_s: float = TICK_DEADLINE_S, clock=time.monotonic,
          events_path: Path | None = None, flows_path: Path | None = None,
          onchain_daily_path: Path | None = None) -> dict:
    t0 = clock()
    # hard bound: deadline_s, cut short at the next weekday 15:00 ET; providers get it as a monotonic value
    deadline_s = max(0.0, (tick_deadline(now_utc, deadline_s) - now_utc).total_seconds())
    deadline_at = time.monotonic() + deadline_s
    since = since or now_utc - timedelta(hours=1)
    events_path = Path(events_path or ROOT / store.EVENTS_PATH)
    flows_path = Path(flows_path or ROOT / store.FLOWS_PATH)
    onchain_daily_path = Path(onchain_daily_path or ROOT / store.ONCHAIN_DAILY_PATH)
    state_path = Path(state_path)
    state = _load_json(state_path)
    tmpdir = tempfile.mkdtemp(prefix='crypto_flow_dry_') if dry_run else None
    work_state = Path(tmpdir) / state_path.name if dry_run else state_path
    if dry_run and state_path.exists():
        shutil.copyfile(state_path, work_state)
    try:
        prev = {} if dry_run or conn is None else store.read_latest_snapshot(conn, with_ts=True)
        slow_prev = prune_slow_obs(state.get('slow_obs') or {}, now_utc)
        hints = price_hints(prev, slow_prev, now_utc)
        results = _run_providers(due_providers(providers, now_utc, state), assets=assets, since=since,
                                 now_utc=now_utc, hints=hints, work_state=work_state,
                                 onchain_daily_path=onchain_daily_path, dry_run=dry_run,
                                 deadline_s=deadline_s, clock=clock, t0=t0, deadline_at=deadline_at)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    by_name = {p.name: p for p in providers}
    events = [e for r in results for e in r.events]
    obs = [o for r in results for o in r.observations]
    slow_obs = update_slow_obs(slow_prev, results, {n: p.cadence for n, p in by_name.items()}, now_utc)
    degraded = [r.provider for r in results if not r.ok]
    skipped = {r.provider: r.skipped for r in results if r.ok and r.skipped}
    ok_names = [r.provider for r in results if r.ok and not r.skipped]
    attempted = [r.provider for r in results if r.skipped not in _NOT_ATTEMPTED]

    if dry_run:
        events_new = len(events)
    else:
        events_new = store.write_events(events, path=events_path)
        if events:
            store.write_events_live(conn, events)

    merged = merge_observations(slow_prev, obs, now_utc=now_utc)
    window = now_utc - timedelta(days=30)
    ev_df = store.read_events_window(window, path=events_path)
    if dry_run and events:
        new = store.events_to_frame(events)
        ev_df = new if ev_df.empty else pd.concat([ev_df, new], ignore_index=True)
    flows_hist = store.read_flows_window(window, path=flows_path)
    snap_hist = None if dry_run or conn is None else store.read_snapshot_history(conn, now_utc - timedelta(days=2))

    active = [a for a in assets if a.role != 'stable']
    signs = {}
    for a in active:
        nf = F.exchange_netflow(ev_df, a.ticker, now_utc, 24)['exch_netflow_usd']
        signs[a.ticker] = (nf > 0) - (nf < 0)
    share = F.agreement_share(signs, signs.get('BTC-USD', 0))
    per = {a.ticker: F.compute_ticker_features(a.ticker, now_utc, ev_df, flows_hist, merged.get(a.ticker, {}), share,
                                               snap_hist=snap_hist) for a in active}
    market_obs = {**merged.get(MARKET_TICKER, {}), 'agreement_share': share, 'providers_failed_this_tick': degraded}
    market = F.compute_market_features(per, market_obs, flows_hist=flows_hist, now=now_utc, ticker_obs=merged)

    frame = build_flows_frame(per, market, now_utc)
    snapshot_rows = 0
    if not dry_run:
        store.write_flows(frame, path=flows_path)
        snap_ts = pd.Timestamp(now_utc).tz_convert('UTC').floor('15min').to_pydatetime()
        snapshot_rows = store.write_snapshot(conn, snap_ts, {**per, MARKET_TICKER: market})
        store.write_provider_health(conn, now_utc, results + [ProviderResult(
            TICK_PROVIDER, True, [], [], len(frame), int((clock() - t0) * 1000))])
    for line in market.get('shock_lines') or []:
        print(f'[crypto-flow] {line}')
    if not dry_run:
        _save_state(state_path, {n: now_utc.isoformat() for n in ok_names},
                    {n: now_utc.isoformat() for n in attempted}, slow_obs)

    c = {'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'), 'providers_ok': len(ok_names),
         'providers_failed': len(degraded), 'providers_skipped': len(skipped), 'events_new': events_new,
         'flows_rows': len(frame), 'snapshot_rows': snapshot_rows, 'degraded': degraded,
         'skipped': list(skipped), 'skipped_reasons': skipped, 'dt_s': round(clock() - t0, 1)}
    _summary(c)
    if skipped:
        print('[crypto-flow] skip reasons: ' + '; '.join(f'{k}={v}' for k, v in skipped.items()))
    for r in results:
        if not r.ok:
            print(f'[crypto-flow] provider_failed {r.provider}: {r.err}')
    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='no parquet/DB writes, no Santiment, no state save')
    ap.add_argument('--force', action='store_true',
                    help='ignore the compute-window / run-lock / compute-process / low-memory skips (not the gate)')
    ap.add_argument('--providers', default='', help='comma-separated provider names to run (default: all)')
    ap.add_argument('--since-hours', type=float, default=1.0)
    args = ap.parse_args(argv)
    if os.environ.get('OPENCLAW_CRYPTO_FLOWS') != '1':
        print('[crypto-flow] gate OFF (OPENCLAW_CRYPTO_FLOWS != 1) — no-op')
        return 0
    try:
        os.nice(19)
    except OSError:
        pass
    now = datetime.now(timezone.utc)
    reason = should_skip(now, force=args.force)
    if reason:
        print(f'[crypto-flow] skipped={reason}')
        return 0
    from src.ingestion.crypto_flow.providers import all_providers
    from src.ingestion.crypto_flow.universe import load_universe
    providers = all_providers()
    if args.providers:
        want = {n.strip() for n in args.providers.split(',') if n.strip()}
        unknown = want - {p.name for p in providers}
        if unknown:
            print(f'[crypto-flow] ERROR unknown provider(s): {sorted(unknown)}', file=sys.stderr)
            return 2
        providers = [p for p in providers if p.name in want]
    conn = None
    if not args.dry_run:
        try:
            conn = store.get_conn()
        except Exception as e:  # noqa: BLE001
            print(f'[crypto-flow] ERROR database unavailable: {type(e).__name__}: {e}', file=sys.stderr)
            return 1
    deadline = float(os.environ.get('OPENCLAW_CRYPTO_FLOW_DEADLINE_S') or TICK_DEADLINE_S)
    try:
        run_tick(now_utc=now, assets=load_universe(), providers=providers, conn=conn, state_path=STATE_PATH,
                 dry_run=args.dry_run, since=now - timedelta(hours=args.since_hours), deadline_s=deadline,
                 install_sigterm=True)
    except TickTerminated:
        return 143
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
