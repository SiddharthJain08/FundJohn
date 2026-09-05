"""Open-interest features from the CBOE chain partitions (spec 2026-09-04 Part B).

Alpaca's snapshots never carry open interest; CBOE's delayed chains do
(data/master/cboe_chains/date=<session>.parquet since 2026-08-21). Point in
time: for a decision on `as_of` the latest CBOE session STRICTLY before
as_of (T−1 for the 15:00 ET compute; the same rule for a backtest bar).
"""
from __future__ import annotations

import datetime as dt
import functools
import os
import re
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
ROOT_ENV = 'OPENCLAW_CBOE_CHAINS_ROOT'
FRONT_DTE_MAX = 45
OI_KEYS = ['max_pain', 'contracts_liquid', 'gex', 'pcr_oi', 'iv_centroid_delta', 'surface_premium', 'oi_session']
_COLS = ['underlying', 'expiry', 'option_type', 'strike', 'open_interest', 'iv', 'delta', 'gamma', 'vega', 'underlying_price']
_PART_RE = re.compile(r'^date=(\d{4}-\d{2}-\d{2})\.parquet$')


def cboe_root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or (ROOT / 'data' / 'master' / 'cboe_chains'))


def clear_cache() -> None:
    _sessions.cache_clear()
    _load.cache_clear()


@functools.lru_cache(maxsize=4)
def _sessions(root_str: str) -> tuple:
    root = Path(root_str)
    if not root.exists():
        return ()
    out = []
    for p in root.iterdir():
        m = _PART_RE.match(p.name)
        if m:
            out.append(dt.date.fromisoformat(m.group(1)))
    return tuple(sorted(out))


def cboe_session_for(as_of, root: Path | None = None) -> dt.date | None:
    d = pd.Timestamp(as_of).date()
    prior = [s for s in _sessions(str(root or cboe_root())) if s < d]
    return prior[-1] if prior else None


@functools.lru_cache(maxsize=2)
def _load(path_str: str) -> pd.DataFrame:
    df = pq.read_table(path_str, columns=_COLS).to_pandas()
    df['underlying'] = df['underlying'].astype(str)
    df['expiry'] = pd.to_datetime(df['expiry'])
    df['option_type'] = df['option_type'].astype(str).str.upper().str[0]
    return df


def load_cboe_session(session: dt.date, tickers=None, root: Path | None = None) -> pd.DataFrame:
    path = Path(root or cboe_root()) / f'date={session.isoformat()}.parquet'
    if not path.exists():
        return pd.DataFrame(columns=_COLS)
    df = _load(str(path))
    return df[df['underlying'].isin(set(tickers))] if tickers is not None else df


def _empty(session) -> dict:
    return {'open_interest_by_strike': {}, 'max_pain': None, 'contracts_liquid': None, 'gex': None,
            'pcr_oi': None, 'iv_centroid_delta': None, 'surface_premium': None,
            'oi_session': session.isoformat() if session else None}


def oi_features_for_day(rows: pd.DataFrame, as_of) -> dict:
    """OI features for ONE underlying from ONE CBOE session (spec B.2)."""
    if rows is None or rows.empty:
        return _empty(None)
    session = cboe_session_for(as_of)
    out = _empty(session)
    r = rows.copy()
    r['open_interest'] = pd.to_numeric(r['open_interest'], errors='coerce').fillna(0.0)
    r['strike'] = pd.to_numeric(r['strike'], errors='coerce')
    as_of_ts = pd.Timestamp(as_of).normalize()
    r['dte'] = (pd.to_datetime(r['expiry']).dt.normalize() - as_of_ts).dt.days
    r = r[r['dte'] >= 1]
    if r.empty:
        return out
    calls_all, puts_all = r[r['option_type'] == 'C'], r[r['option_type'] == 'P']
    coi, poi = float(calls_all['open_interest'].sum()), float(puts_all['open_interest'].sum())
    out['pcr_oi'] = (poi / coi) if coi > 0 else None
    front = r[r['dte'] <= FRONT_DTE_MAX]
    if front.empty:
        front = r
    fr = front[front['dte'] == front['dte'].min()]
    by_strike = fr.groupby('strike')['open_interest'].sum()
    by_strike = by_strike[by_strike > 0]
    out['open_interest_by_strike'] = {float(k): float(v) for k, v in by_strike.items()}
    out['contracts_liquid'] = int((fr['open_interest'] > 0).sum())
    calls, puts = fr[fr['option_type'] == 'C'], fr[fr['option_type'] == 'P']
    if len(by_strike):
        ks = sorted(by_strike.index)
        best, best_pay = None, None
        for s in ks:
            pay = float(((s - calls['strike']).clip(lower=0) * calls['open_interest']).sum()
                        + ((puts['strike'] - s).clip(lower=0) * puts['open_interest']).sum())
            if best_pay is None or pay < best_pay:
                best, best_pay = float(s), pay
        out['max_pain'] = best
    gc = float((pd.to_numeric(calls['gamma'], errors='coerce').fillna(0) * calls['open_interest']).sum())
    gp = float((pd.to_numeric(puts['gamma'], errors='coerce').fillna(0) * puts['open_interest']).sum())
    out['gex'] = round((gc - gp) * 100, 2) if (coi + poi) > 0 else None
    w = pd.to_numeric(fr['vega'], errors='coerce').abs().fillna(0) * fr['open_interest']
    tw = float(w.sum())
    if tw > 0:
        d = pd.to_numeric(fr['delta'], errors='coerce').fillna(0)
        iv = pd.to_numeric(fr['iv'], errors='coerce').fillna(0)
        out['iv_centroid_delta'] = round(float((d * w).sum() / tw), 4)
        vwiv = float((iv * w).sum() / tw)
        atm5 = fr[pd.to_numeric(fr['delta'], errors='coerce').abs().between(0.45, 0.55)]
        atm_iv5 = pd.to_numeric(atm5['iv'], errors='coerce').dropna()
        out['surface_premium'] = round(vwiv - (float(atm_iv5.mean()) if len(atm_iv5) else vwiv), 4)
    return out


def oi_features_for_ticker(ticker: str, as_of, master_dir=None) -> dict:
    root = (Path(master_dir) / 'cboe_chains') if master_dir and not os.environ.get(ROOT_ENV) else cboe_root()
    session = cboe_session_for(as_of, root)
    if session is None:
        return _empty(None)
    rows = load_cboe_session(session, [ticker], root)
    if rows.empty:
        return _empty(session)
    return oi_features_for_day(rows, as_of)


def oi_lookup_factory(root: Path | None = None):
    """(ticker, date) -> scalar OI keys for the surface-master builder, or None
    when no CBOE session precedes the date."""
    def look(ticker: str, day) -> dict | None:
        session = cboe_session_for(day, root)
        if session is None:
            return None
        rows = load_cboe_session(session, [ticker], root)
        f = oi_features_for_day(rows, day) if not rows.empty else _empty(session)
        return {k: f[k] for k in OI_KEYS}
    return look
