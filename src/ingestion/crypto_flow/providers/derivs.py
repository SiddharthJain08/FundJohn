"""derivs: perp positioning (OI, funding, premium, L/S and taker ratios). Hyperliquid is primary;
Binance (top-N by HL volume) supplies ratios, Bybit/OKX are fallbacks for ratios and OI."""
from __future__ import annotations

import math
import os
import time

from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

HL_URL = 'https://api.hyperliquid.xyz/info'
BINANCE = 'https://fapi.binance.com'
BYBIT = 'https://api.bybit.com/v5/market'
OKX_OI_URL = 'https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={}'
ALWAYS = ('BTC-USD', 'ETH-USD')
FIELDS = ('oi_usd', 'funding_8h', 'premium', 'mark_px', 'vol_24h_usd', 'ls_ratio_global', 'ls_ratio_top',
          'taker_buy_sell', 'oi_usd_total', 'oi_venues', 'venues', 'px_source')
BINANCE_SUPPLEMENTS = (  # (field, path, json key)
    ('ls_ratio_global', '/futures/data/globalLongShortAccountRatio', 'longShortRatio'),
    ('ls_ratio_top', '/futures/data/topLongShortPositionRatio', 'longShortRatio'),
    ('taker_buy_sell', '/futures/data/takerlongshortRatio', 'buySellRatio'))


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _unit_multiplier(venue_symbol: str | None) -> int:
    """Tokens per contract unit: Hyperliquid kXXX and Bybit 1000XXX / XXX1000USDT are priced per 1000 tokens."""
    s = venue_symbol or ''
    if s.startswith('k') and s[1:].isupper():
        return 1000
    return 1000 if s.startswith('1000') or s.endswith('1000USDT') else 1


def top_n() -> int:
    return int(os.environ.get('OPENCLAW_CRYPTO_DERIVS_BINANCE_TOP_N', 12))


def fetch_hl():
    return get_json(HL_URL, label='hyperliquid_meta', timeout=15, method='POST',
                    body={'type': 'metaAndAssetCtxs'})


def parse_hl(payload, assets) -> dict[str, dict]:
    """ticker -> {oi_usd, funding_8h, premium, mark_px (per token), vol_24h_usd} for assets listed on HL."""
    try:
        meta, ctxs = payload
        by_name = {m['name']: c for m, c in zip(meta['universe'], ctxs)}
    except (TypeError, ValueError, KeyError):
        return {}
    out = {}
    for a in assets:
        c = by_name.get(a.hyperliquid) if a.hyperliquid else None
        if not c:
            continue
        mark, oi, fund = _f(c.get('markPx')), _f(c.get('openInterest')), _f(c.get('funding'))
        out[a.ticker] = {
            'oi_usd': oi * mark if oi is not None and mark is not None else None,  # unit-invariant
            'funding_8h': fund * 8 if fund is not None else None,  # HL funding is hourly
            'premium': _f(c.get('premium')),
            'mark_px': mark / _unit_multiplier(a.hyperliquid) if mark is not None else None,
            'vol_24h_usd': _f(c.get('dayNtlVlm'))}
    return out


def select_top(assets, vols: dict, attr: str, n: int) -> list:
    """Non-stable assets with a `attr` venue symbol: BTC/ETH always, rest by HL volume, capped at n."""
    pool = [a for a in assets if a.role != 'stable' and getattr(a, attr)]
    forced = [a for a in pool if a.ticker in ALWAYS]
    rest = sorted((a for a in pool if a.ticker not in ALWAYS), key=lambda a: -(vols.get(a.ticker) or 0))
    return forced + rest[:max(0, n - len(forced))]


def _first(payload, key):
    row = payload[0] if isinstance(payload, list) and payload else None
    return _f(row.get(key)) if isinstance(row, dict) else None


def parse_okx_oi(payload) -> float | None:
    try:
        return _f(payload['data'][0]['oiUsd'])
    except (TypeError, KeyError, IndexError):
        return None


def parse_bybit_ratio(payload) -> float | None:
    try:
        row = payload['result']['list'][0]
        buy, sell = _f(row['buyRatio']), _f(row['sellRatio'])
    except (TypeError, KeyError, IndexError):
        return None
    return buy / sell if buy is not None and sell else None


def parse_bybit_oi(payload) -> float | None:
    try:
        return _f(payload['result']['list'][0]['openInterest'])
    except (TypeError, KeyError, IndexError):
        return None


class Derivs(Provider):
    name = 'derivs'
    cadence = 'tick'

    def _binance(self, a, rec) -> bool:
        """Fill Binance fields; False if the openInterest call failed."""
        sym = a.binance
        oi = get_json(f'{BINANCE}/fapi/v1/openInterest?symbol={sym}', label='binance_oi', timeout=15)
        if oi is None:
            return False
        prem = get_json(f'{BINANCE}/fapi/v1/premiumIndex?symbol={sym}', label='binance_premium', timeout=15)
        mark = _f((prem or {}).get('markPrice'))
        coins = _f(oi.get('openInterest')) if isinstance(oi, dict) else None
        if coins is not None and mark is not None:
            rec['oi'].append(('binance', coins * mark))
        if rec['mark_px'] is None and mark is not None:
            rec['mark_px'], rec['px_source'] = mark, 'binance'
        if rec['funding_8h'] is None:
            rec['funding_8h'] = _f((prem or {}).get('lastFundingRate'))
        got = coins is not None and mark is not None
        for fld, path, key in BINANCE_SUPPLEMENTS:
            time.sleep(0.1)
            v = _first(get_json(f'{BINANCE}{path}?symbol={sym}&period=1h&limit=1', label=f'binance_{fld}',
                                timeout=15), key)
            rec[fld] = v
            got = got or v is not None
        if got:
            rec['venues'].append('binance')
        return True

    def _bybit(self, a, rec) -> None:
        if not a.bybit:
            return
        r = get_json(f'{BYBIT}/account-ratio?category=linear&symbol={a.bybit}&period=1h&limit=1',
                     label='bybit_ratio', timeout=15)
        rec['ls_ratio_global'] = parse_bybit_ratio(r)
        coins = parse_bybit_oi(get_json(f'{BYBIT}/open-interest?category=linear&symbol={a.bybit}'
                                        '&intervalTime=1h&limit=1', label='bybit_oi', timeout=15))
        if coins is not None and rec['mark_px'] is not None:
            rec['oi'].append(('bybit', coins * _unit_multiplier(a.bybit) * rec['mark_px']))
        if rec['ls_ratio_global'] is not None or coins is not None:
            rec['venues'].append('bybit')

    def _okx(self, a, rec) -> None:
        v = parse_okx_oi(get_json(OKX_OI_URL.format(a.okx), label='okx_oi', timeout=15))
        if v is not None:
            rec['oi'].append(('okx', v))
            rec['venues'].append('okx')

    def _fetch(self, assets, since, now):
        hl_payload = fetch_hl()
        if hl_payload is None:
            raise ProviderFetchError('derivs: hyperliquid None')
        hl = parse_hl(hl_payload, assets)
        vols = {t: d['vol_24h_usd'] for t, d in hl.items()}
        recs: dict[str, dict] = {}
        for a in assets:
            if a.role == 'stable':
                continue
            rec = {f: None for f in FIELDS} | {'oi': [], 'venues': []}
            if a.ticker in hl:
                rec |= hl[a.ticker]
                rec['px_source'] = 'hyperliquid'
                rec['venues'].append('hyperliquid')
                if rec['oi_usd'] is not None:
                    rec['oi'].append(('hyperliquid', rec['oi_usd']))
            recs[a.ticker] = rec
        n_top = top_n()
        degraded = False
        for i, a in enumerate(select_top(assets, vols, 'binance', n_top)):
            ok = self._binance(a, recs[a.ticker])
            if i == 0 and not ok:  # first call failed: Binance is down, stop hammering it
                degraded = True
                break
            time.sleep(0.1)
        if degraded:
            for a in select_top(assets, vols, 'bybit', n_top):
                self._bybit(a, recs[a.ticker])
        okx_top = {a.ticker for a in select_top(assets, vols, 'okx', n_top)}
        for a in assets:  # OKX OI where fewer than 2 venues gave OI (and for assets HL lacks)
            rec = recs.get(a.ticker)
            if rec is not None and a.okx and len(rec['oi']) < 2 and (a.ticker not in hl or a.ticker in okx_top):
                self._okx(a, rec)
        obs = []
        for a in assets:
            rec = recs.get(a.ticker)
            if not rec or not rec['venues']:
                continue
            oi = rec.pop('oi')
            if rec['oi_usd'] is None and oi:
                rec['oi_usd'] = oi[0][1]
            rec['oi_usd_total'] = sum(v for _, v in oi) if len(oi) >= 2 else None
            rec['oi_venues'] = ','.join(k for k, _ in oi) or None
            rec['venues'] = ','.join(rec['venues'])
            obs.append(Observation(now, a.ticker, self.name, {f: rec[f] for f in FIELDS}))
        if degraded:
            obs.append(Observation(now, MARKET_TICKER, self.name, {'binance_degraded': True}))
        return [], obs
