"""spot_tape: large-print detection on spot exchange tapes (Binance -> Coinbase -> Kraken fallback).
Every adapter normalises to (price, size, taker_side, ts_epoch_s); taker_side is 'buy' | 'sell'."""
from __future__ import annotations

import os
import time
from datetime import datetime

from src.ingestion.crypto_flow.models import Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json, past_deadline
from src.ingestion.crypto_flow.providers.derivs import fetch_hl, parse_hl, select_top

BINANCE = 'https://api.binance.com/api/v3/aggTrades?symbol={}&limit=1000'
COINBASE = 'https://api.exchange.coinbase.com/products/{}/trades?limit=1000'
KRAKEN = 'https://api.kraken.com/0/public/Trades?pair={}'


def large_print_usd() -> float:
    return float(os.environ.get('OPENCLAW_CRYPTO_LARGE_PRINT_USD', 250_000))


def top_n() -> int:
    return int(os.environ.get('OPENCLAW_CRYPTO_TAPE_TOP_N', 12))


def classify_prints(trades: list[tuple], min_usd: float) -> dict:
    """trades: (price, size, taker_side, ...). Counts prints with notional >= min_usd by taker side."""
    buy = sell = 0.0
    n = 0
    for t in trades:
        notional = t[0] * t[1]
        if notional < min_usd:
            continue
        n += 1
        if t[2] == 'buy':
            buy += notional
        else:
            sell += notional
    tot = buy + sell
    return {'large_prints_n': n, 'large_prints_usd_buy': buy, 'large_prints_usd_sell': sell,
            'large_print_imbalance_1h': (buy - sell) / tot if tot else None}


def binance_trades(payload) -> list[tuple]:
    """`m` = buyer was the maker => the TAKER sold."""
    try:
        return [(float(r['p']), float(r['q']), 'sell' if r['m'] else 'buy', r['T'] / 1000) for r in payload]
    except (TypeError, KeyError, ValueError):
        return []


def coinbase_trades(payload) -> list[tuple]:
    """Coinbase `side` is the MAKER side: 'sell' => a taker bought (up-tick); 'buy' => taker sold."""
    try:
        return [(float(r['price']), float(r['size']), 'buy' if r['side'] == 'sell' else 'sell',
                 datetime.fromisoformat(r['time']).timestamp()) for r in payload]
    except (TypeError, KeyError, ValueError):
        return []


def kraken_trades(payload, pair: str) -> list[tuple] | None:
    """Entries [price, vol, time, 'b'|'s', ...]; b/s is the TAKER side. None when Kraken reports an error.
    The result key is Kraken's canonical name (XXBTZUSD for XBTUSD), so fall back to the only non-'last' key."""
    try:
        if payload['error']:
            return None
        res = payload['result']
        rows = res.get(pair) or next(v for k, v in res.items() if k != 'last')
        return [(float(r[0]), float(r[1]), 'buy' if r[3] == 'b' else 'sell', float(r[2])) for r in rows]
    except (TypeError, KeyError, ValueError, StopIteration):
        return None


def kraken_pair(a) -> str:
    base = a.ticker.split('-')[0]
    return ('XBT' if base == 'BTC' else base) + 'USD'


class SpotTape(Provider):
    name = 'spot_tape'
    cadence = 'tick'
    deadline_at: float | None = None  # time.monotonic() bound, set by the tick driver
    deadline = 0  # assets not fetched because the tick deadline hit

    def _tape(self, a, venues_down: set) -> tuple[str, list[tuple]] | None:
        """First venue that returns a non-empty tape for this asset; flags Binance down on a None."""
        if past_deadline(self.deadline_at):
            return None
        if a.binance and 'binance' not in venues_down:
            r = get_json(BINANCE.format(a.binance), label='spot_binance', timeout=15)
            if r is None:
                venues_down.add('binance')
            elif binance_trades(r):
                return 'binance', binance_trades(r)
            time.sleep(0.1)
        if a.coinbase and not past_deadline(self.deadline_at):
            t = coinbase_trades(get_json(COINBASE.format(a.coinbase), label='spot_coinbase', timeout=15))
            time.sleep(0.1)
            if t:
                return 'coinbase', t
        if past_deadline(self.deadline_at):
            return None
        pair = kraken_pair(a)
        t = kraken_trades(get_json(KRAKEN.format(pair), label='spot_kraken', timeout=15), pair)
        time.sleep(0.1)
        return ('kraken', t) if t else None

    def _fetch(self, assets, since, now):
        vols = {t: d['vol_24h_usd'] for t, d in parse_hl(fetch_hl(), assets).items()}
        min_usd, down, obs = large_print_usd(), set(), []
        self.deadline = 0
        top = select_top(assets, vols, 'ticker', top_n())
        for i, a in enumerate(top):
            if past_deadline(self.deadline_at):
                self.deadline = len(top) - i
                print(f'[spot_tape] deadline=1 assets_seen={i} assets_skipped={self.deadline}')
                break
            got = self._tape(a, down)
            if not got:
                continue
            venue, trades = got
            ts = [t[3] for t in trades]
            obs.append(Observation(now, a.ticker, self.name, classify_prints(trades, min_usd) | {
                'tape_coverage_s': max(ts) - min(ts), 'tape_venue': venue, 'tape_n': len(trades)}))
        if not obs and not self.deadline:
            raise ProviderFetchError('spot_tape: no venue answered')
        return [], obs
