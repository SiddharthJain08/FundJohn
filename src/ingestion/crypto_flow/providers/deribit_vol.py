"""deribit_vol: BTC/ETH implied volatility (DVOL, 30d ATM IV, wing skew, put/call OI) from Deribit."""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta, timezone

from src.ingestion.crypto_flow.models import Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

BASE = 'https://www.deribit.com/api/v2/public'
CURRENCIES = {'BTC': 'BTC-USD', 'ETH': 'ETH-USD'}
MIN_DTE, TARGET_DTE = 7, 30
WING = 0.10


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_instrument(name: str) -> tuple[str, date, float, str]:
    """'BTC-26DEC26-80000-C' -> ('BTC', date(2026, 12, 26), 80000.0, 'C'). ValueError if malformed."""
    cur, exp, strike, kind = name.split('-')
    if kind not in ('C', 'P'):
        raise ValueError(name)
    return cur, datetime.strptime(exp.title(), '%d%b%y').date(), float(strike), kind


def summarize_options(rows, now: datetime) -> dict:
    """Option-chain stats from get_book_summary_by_currency rows (mark_iv in percent).

    atm_iv_30d: expiry with days-to-expiry (08:00 UTC settle) nearest 30 and >= 7 (weeklies are noisy),
      strike nearest that expiry's underlying_price, mean of call and put mark_iv.
    skew_25d: APPROXIMATION - the book summary carries no delta, so the 25-delta wings are taken as the
      put at the strike nearest 0.90 x underlying and the call nearest 1.10 x underlying on the same
      expiry; skew_25d = put_iv - call_iv.
    put_call_oi: sum put open_interest / sum call open_interest over all expiries (None if no call OI)."""
    parsed, oi = [], {'C': 0.0, 'P': 0.0}
    for r in rows or []:
        try:
            _, exp, strike, kind = parse_instrument(r['instrument_name'])
        except (ValueError, KeyError, TypeError):
            continue
        oi[kind] += _f(r.get('open_interest')) or 0.0
        iv = _f(r.get('mark_iv'))
        parsed.append((exp, strike, kind, iv if iv and iv > 0 else None, _f(r.get('underlying_price'))))
    out = {'atm_iv_30d': None, 'skew_25d': None, 'atm_expiry': None, 'n_options': len(parsed),
           'put_call_oi': oi['P'] / oi['C'] if oi['C'] > 0 else None}

    def dte(exp):
        return (datetime.combine(exp, dtime(8), tzinfo=timezone.utc) - now) / timedelta(days=1)
    cands = {p[0] for p in parsed if dte(p[0]) >= MIN_DTE}
    if not cands:
        return out
    exp = min(cands, key=lambda e: abs(dte(e) - TARGET_DTE))
    chain = [p for p in parsed if p[0] == exp and p[3] is not None]
    spot = next((p[4] for p in chain if p[4]), None)
    if not chain or not spot:
        return out
    out['atm_expiry'] = exp.isoformat()

    def iv_at(target, kinds):
        pool = [p for p in chain if p[2] in kinds]
        if not pool:
            return None
        strike = min({p[1] for p in pool}, key=lambda k: abs(k - target))
        ivs = [p[3] for p in pool if p[1] == strike]
        return sum(ivs) / len(ivs)
    out['atm_iv_30d'] = iv_at(spot, 'CP')
    put, call = iv_at(spot * (1 - WING), 'P'), iv_at(spot * (1 + WING), 'C')
    out['skew_25d'] = put - call if put is not None and call is not None else None
    return out


def parse_dvol(payload) -> float | None:
    try:
        return _f(payload['result']['data'][-1][4])
    except (TypeError, KeyError, IndexError):
        return None


class DeribitVol(Provider):
    name = 'deribit_vol'
    cadence = 'tick'

    def _fetch(self, assets, since, now):
        ms = int(now.timestamp() * 1000)
        obs, failed = [], []
        for cur, ticker in CURRENCIES.items():
            dvol = parse_dvol(get_json(
                f'{BASE}/get_volatility_index_data?currency={cur}&resolution=3600'
                f'&start_timestamp={ms - 7_200_000}&end_timestamp={ms}', label=f'deribit_dvol_{cur}', timeout=15))
            if dvol is None:
                failed.append(cur)
                continue
            book = get_json(f'{BASE}/get_book_summary_by_currency?currency={cur}&kind=option',
                            label=f'deribit_book_{cur}', timeout=15)
            rows = book.get('result') if isinstance(book, dict) else None
            degraded = rows is None
            obs.append(Observation(now, ticker, self.name, {'dvol': dvol} | summarize_options(rows, now)
                                   | {'options_degraded': degraded}))
        if len(failed) == len(CURRENCIES):
            raise ProviderFetchError('deribit_vol: dvol None')
        return [], obs
