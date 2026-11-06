#!/usr/bin/env python3
"""15-minute crypto flow tick: run due providers → whale-event archive → per-ticker + market
features → crypto_flows_1h.parquet + crypto_flow_snapshot + provider health.

Gated on OPENCLAW_CRYPTO_FLOWS=1 (scheduler job `crypto-flow-tick`, cron-schedule.js). Skips the
15:00–15:50 ET weekday compute window and any running pipeline_orchestrator.py (--force overrides
the skips, never the gate). Providers run strictly sequentially (they share one state file) under
a tick deadline (OPENCLAW_CRYPTO_FLOW_DEADLINE_S, default 600 s).

--dry-run: no parquet / DB writes, Santiment not run, real state file untouched (providers get a
throw-away copy, so on-chain cursors do not advance), DB not contacted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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

STATE_PATH = ROOT / 'data/master/.crypto_flow_state.json'
TICK_DEADLINE_S = 600
_ET = ZoneInfo('America/New_York')
_MIN_GAP = {'hourly': timedelta(minutes=55), 'daily': timedelta(hours=23)}
_STR_COLS = {'oi_venues', 'oi_price_divergence', 'whale_direction', 'whale_direction_btc', 'cot_report_date',
             'providers_degraded'}
_BOOL_COLS = {'delev_1h', 'delev_market'}
_SNAPSHOT_ONLY = {'shock_lines', 'whale_score_components'}  # kept in the snapshot JSON, not the parquet


def _pgrep(pattern: str) -> bool:
    return subprocess.run(['pgrep', '-f', pattern], capture_output=True).returncode == 0


def should_skip(now_utc: datetime, *, pgrep=_pgrep, force: bool = False) -> str | None:
    if force:
        return None
    et = now_utc.astimezone(_ET)
    if et.weekday() < 5 and 15 * 60 <= et.hour * 60 + et.minute < 15 * 60 + 50:
        return 'compute_window'
    if pgrep('pipeline_orchestrator.py'):
        return 'orchestrator_running'
    return None


def due_providers(providers: list, now_utc: datetime, state: dict) -> list:
    """'tick' always; 'hourly' when never run or ≥ 55 min ago; 'daily' when ≥ 23 h ago."""
    last = (state or {}).get('last_run') or {}
    out = []
    for p in providers:
        gap, prev = _MIN_GAP.get(p.cadence), last.get(p.name)
        try:
            if gap is None or not prev or now_utc - datetime.fromisoformat(prev) >= gap:
                out.append(p)
        except (TypeError, ValueError):
            out.append(p)
    return out


def merge_observations(prev: dict[str, dict], observations: list, *, slow_keys: set) -> dict[str, dict]:
    """Per ticker: the previous snapshot's hourly/daily-provider fields (`slow_keys`), overlaid by this tick's
    observation fields in provider order (later providers win on the same key)."""
    merged = {tk: {k: v for k, v in (f or {}).items() if k in slow_keys} for tk, f in prev.items()}
    for o in observations:
        merged.setdefault(o.ticker, {}).update(o.fields)
    return merged


def build_flows_frame(per_ticker: dict[str, dict], market: dict, now_utc: datetime) -> pd.DataFrame:
    """One row per ticker + __MARKET__, ts_utc floored to the hour, stable dtypes for append_dedup merges."""
    mkt = {k: v for k, v in market.items() if k not in _SNAPSHOT_ONLY}
    mkt['providers_degraded'] = ','.join(market.get('providers_degraded') or [])
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


def _save_state(path: Path, last_run: dict, slow_keys: set) -> None:
    """Re-read the file (providers wrote their cursors into it during the tick) and merge only our keys."""
    state = _load_json(path)
    state['last_run'] = {**(state.get('last_run') or {}), **last_run}
    state['slow_keys'] = sorted(slow_keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _run_providers(due, *, assets, since, now_utc, hints, work_state, onchain_daily_path, dry_run,
                   deadline_s, clock, t0):
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
        results.append(run_provider(p, assets, since, now_utc))
    return results


def run_tick(*, now_utc: datetime, assets, providers, conn, state_path: Path, dry_run: bool,
             since: datetime | None = None, deadline_s: float = TICK_DEADLINE_S, clock=time.monotonic,
             events_path: Path | None = None, flows_path: Path | None = None,
             onchain_daily_path: Path | None = None) -> dict:
    t0 = clock()
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
        prev = {} if dry_run or conn is None else store.read_latest_snapshot(conn)
        hints = {tk: F._f(f.get('price_usd')) for tk, f in prev.items()}
        hints = {tk: v for tk, v in hints.items() if v}
        results = _run_providers(due_providers(providers, now_utc, state), assets=assets, since=since,
                                 now_utc=now_utc, hints=hints, work_state=work_state,
                                 onchain_daily_path=onchain_daily_path, dry_run=dry_run,
                                 deadline_s=deadline_s, clock=clock, t0=t0)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    by_name = {p.name: p for p in providers}
    events = [e for r in results for e in r.events]
    obs = [o for r in results for o in r.observations]
    slow_keys = set(state.get('slow_keys') or [])
    slow_keys |= {k for r in results if by_name[r.provider].cadence != 'tick'
                  for o in r.observations for k in o.fields}
    degraded = [r.provider for r in results if not r.ok]
    skipped = {r.provider: r.skipped for r in results if r.ok and r.skipped}
    ok_names = [r.provider for r in results if r.ok and not r.skipped]

    if dry_run:
        events_new = len(events)
    else:
        events_new = store.write_events(events, path=events_path)
        if events:
            store.write_events_live(conn, events)

    merged = merge_observations(prev, obs, slow_keys=slow_keys)
    window = now_utc - timedelta(days=30)
    ev_df = store.read_events_window(window, path=events_path)
    if dry_run and events:
        new = store.events_to_frame(events)
        ev_df = new if ev_df.empty else pd.concat([ev_df, new], ignore_index=True)
    flows_hist = store.read_flows_window(window, path=flows_path)

    active = [a for a in assets if a.role != 'stable']
    signs = {}
    for a in active:
        nf = F.exchange_netflow(ev_df, a.ticker, now_utc, 24)['exch_netflow_usd']
        signs[a.ticker] = (nf > 0) - (nf < 0)
    share = F.agreement_share(signs, signs.get('BTC-USD', 0))
    per = {a.ticker: F.compute_ticker_features(a.ticker, now_utc, ev_df, flows_hist, merged.get(a.ticker, {}), share)
           for a in active}
    market_obs = {**merged.get(MARKET_TICKER, {}), 'agreement_share': share, 'providers_degraded': degraded}
    market = F.compute_market_features(per, market_obs, flows_hist=flows_hist, now=now_utc)

    frame = build_flows_frame(per, market, now_utc)
    snapshot_rows = 0
    if not dry_run:
        store.write_flows(frame, path=flows_path)
        snap_ts = pd.Timestamp(now_utc).tz_convert('UTC').floor('15min').to_pydatetime()
        snapshot_rows = store.write_snapshot(conn, snap_ts, {**per, MARKET_TICKER: market})
        store.write_provider_health(conn, now_utc, results)
    for line in market.get('shock_lines') or []:
        print(f'[crypto-flow] {line}')
    if not dry_run:
        _save_state(state_path, {n: now_utc.isoformat() for n in ok_names}, slow_keys)

    c = {'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'), 'providers_ok': len(ok_names),
         'providers_failed': len(degraded), 'providers_skipped': len(skipped), 'events_new': events_new,
         'flows_rows': len(frame), 'snapshot_rows': snapshot_rows, 'degraded': degraded,
         'skipped': list(skipped), 'skipped_reasons': skipped, 'dt_s': round(clock() - t0, 1)}
    print(f"[crypto-flow] ts={c['ts']} providers_ok={c['providers_ok']} providers_failed={c['providers_failed']} "
          f"providers_skipped={c['providers_skipped']} events_new={c['events_new']} flows_rows={c['flows_rows']} "
          f"snapshot_rows={c['snapshot_rows']} degraded={','.join(degraded) or '-'} "
          f"skipped={','.join(skipped) or '-'} dt_s={c['dt_s']}")
    if skipped:
        print('[crypto-flow] skip reasons: ' + '; '.join(f'{k}={v}' for k, v in skipped.items()))
    for r in results:
        if not r.ok:
            print(f'[crypto-flow] provider_failed {r.provider}: {r.err}')
    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='no parquet/DB writes, no Santiment, no state save')
    ap.add_argument('--force', action='store_true', help='ignore the compute-window / orchestrator skip (not the gate)')
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
    reason = should_skip(now, pgrep=_pgrep, force=args.force)
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
                 dry_run=args.dry_run, since=now - timedelta(hours=args.since_hours), deadline_s=deadline)
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
