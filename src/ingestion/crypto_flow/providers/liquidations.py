"""liquidations: OKX filled liquidation orders over the last hour, split long/short, in USD."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src.ingestion.crypto_flow.models import Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json
from src.ingestion.crypto_flow.providers.derivs import _f, fetch_hl, parse_hl, select_top

LIQ_URL = ('https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&uly={uly}'
           '&state=filled&limit=100')
INSTR_URL = 'https://www.okx.com/api/v5/public/instruments?instType=SWAP'
CTVAL_TTL = 24 * 3600
CTVAL_DEFAULT = {'BTC-USDT-SWAP': 0.01, 'ETH-USDT-SWAP': 0.1}


def parse_okx_instruments(payload) -> dict[str, float]:
    try:
        rows = payload['data']
        return {r['instId']: v for r in rows if (v := _f(r.get('ctVal'))) is not None}
    except (TypeError, KeyError, AttributeError):
        return {}


def parse_okx_liqs(payload, ctval: dict[str, float], now) -> dict[str, dict]:
    """instId -> {liq_long_usd_1h, liq_short_usd_1h, liq_n_1h} over details with ts >= now - 1h.
    Notional = sz * ctVal * bkPx; posSide 'long' is a long liquidation."""
    cutoff_ms = (now.timestamp() - 3600) * 1000
    out = {}
    for row in (payload or {}).get('data', []):
        inst = row.get('instId')
        cv = ctval.get(inst)
        if cv is None:
            continue
        acc = out.setdefault(inst, {'liq_long_usd_1h': 0.0, 'liq_short_usd_1h': 0.0, 'liq_n_1h': 0})
        for d in row.get('details', []):
            ts, sz, px = _f(d.get('ts')), _f(d.get('sz')), _f(d.get('bkPx'))
            if ts is None or sz is None or px is None or ts < cutoff_ms:
                continue
            acc['liq_long_usd_1h' if d.get('posSide') == 'long' else 'liq_short_usd_1h'] += sz * cv * px
            acc['liq_n_1h'] += 1
    return out


class Liquidations(Provider):
    name = 'liquidations'
    cadence = 'tick'
    state_path = Path('data/master/.crypto_flow_state.json')

    def __init__(self):
        self._ctval_cache: dict = {}

    def load_state(self) -> dict:
        try:
            return json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)

    def _ctvals(self) -> tuple[dict[str, float], bool]:
        """(instId -> ctVal, fetch_ok). Cached 24 h in memory and in state under 'okx_ctval'."""
        cache = self._ctval_cache or self.load_state().get('okx_ctval') or {}
        if cache.get('map') and time.time() - cache.get('fetched_at', 0) < CTVAL_TTL:
            self._ctval_cache = cache
            return cache['map'], True
        fresh = parse_okx_instruments(get_json(INSTR_URL, label='okx_instruments', timeout=15))
        if fresh:
            self._ctval_cache = {'fetched_at': time.time(), 'map': fresh}
            self.save_state({**self.load_state(), 'okx_ctval': self._ctval_cache})
            return fresh, True
        return {**CTVAL_DEFAULT, **cache.get('map', {})}, False

    def _fetch(self, assets, since, now):
        ctval, instr_ok = self._ctvals()
        hl = fetch_hl()
        vols = {t: d['vol_24h_usd'] for t, d in parse_hl(hl, assets).items()} if hl else {}
        n = int(os.environ.get('OPENCLAW_CRYPTO_LIQ_TOP_N', 12))
        obs, answered = [], 0
        for a in select_top(assets, vols, 'okx', n):
            payload = get_json(LIQ_URL.format(uly=a.okx.removesuffix('-SWAP')), label='okx_liq', timeout=15)
            if not isinstance(payload, dict) or payload.get('code') != '0':
                continue
            answered += 1
            f = parse_okx_liqs(payload, ctval, now).get(a.okx)
            if f is None and a.okx in ctval:
                f = {'liq_long_usd_1h': 0.0, 'liq_short_usd_1h': 0.0, 'liq_n_1h': 0}
            if f is not None:
                obs.append(Observation(now, a.ticker, self.name, f))
        if not instr_ok and not answered:
            raise ProviderFetchError('liquidations: okx instruments and liquidation calls all failed')
        return [], obs
