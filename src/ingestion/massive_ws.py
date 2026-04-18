"""
Massive WebSocket client — options data only (options starter plan).

Feed: wss://socket.massive.com/options  (real-time)
      wss://delayed.massive.com/options  (15-min delayed)

Auth flow:
  connect → {"ev":"status","status":"connected"} →
  send {"action":"auth","params":"API_KEY"} →
  receive {"ev":"status","status":"auth_success"} →
  send {"action":"subscribe","params":"OA.*"} →
  stream

OA event fields (options per-minute aggregate):
  ev, sym (OCC format: O:AAPL240117C00150000), v, o, c, h, l, a, s, e

Classes:
  MassiveWSClient        — generic reconnecting WS client
  MassiveOptionsCapture  — accumulates OA.* volume, writes unusual_flow to Redis
"""

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

MASSIVE_RT_BASE      = os.environ.get('MASSIVE_WS_REALTIME_BASE', 'wss://socket.massive.com')
MASSIVE_DELAYED_BASE = os.environ.get('MASSIVE_WS_DELAYED_BASE',  'wss://delayed.massive.com')


def _ws_url(realtime: bool = True) -> str:
    base = MASSIVE_RT_BASE if realtime else MASSIVE_DELAYED_BASE
    return f'{base}/options'


# ---------------------------------------------------------------------------
# OCC symbol parser  O:AAPL240117C00150000
# ---------------------------------------------------------------------------

_OCC_RE = re.compile(r'^O:([A-Z.]+)(\d{6})([CP])(\d{8})$')


def _parse_occ(sym: str) -> Optional[Tuple[str, str, str, float]]:
    """
    Parse OCC option symbol into (underlying, expiry, contract_type, strike).
    e.g. 'O:AAPL240117C00150000' → ('AAPL', '2024-01-17', 'call', 150.0)
    Returns None if sym is not a valid OCC symbol.
    """
    m = _OCC_RE.match((sym or '').upper())
    if not m:
        return None
    und, date6, cp, strike8 = m.groups()
    expiry = f'20{date6[:2]}-{date6[2:4]}-{date6[4:]}'
    return und, expiry, 'call' if cp == 'C' else 'put', int(strike8) / 1000.0


# ---------------------------------------------------------------------------
# MassiveWSClient
# ---------------------------------------------------------------------------

class MassiveWSClient:
    """
    Reconnecting Massive WebSocket client for the options feed.

    Usage:
        async def handler(ev_type: str, sym: str, event: dict): ...
        client = MassiveWSClient(['OA.*'], handler)
        await client.connect()   # blocks until disconnect() is called
    """

    def __init__(
        self,
        channels:   List[str],
        on_message: Callable,
        api_key:    str  = None,
        realtime:   bool = True,
    ):
        self.channels   = channels
        self.on_message = on_message
        self.api_key    = (api_key
                          or os.environ.get('MASSIVE_SECRET_KEY', '')
                          or os.environ.get('POLYGON_API_KEY', ''))
        self._ws_url    = _ws_url(realtime)
        self._running   = False

    async def connect(self):
        """Connect, authenticate, subscribe, stream. Auto-reconnects on failure."""
        try:
            import websockets
        except ImportError:
            logger.error('massive_ws requires websockets: pip install websockets')
            return

        self._running = True
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    backoff = 1

                    # 1. Wait for connected event
                    raw    = await asyncio.wait_for(ws.recv(), timeout=10)
                    events = json.loads(raw)
                    evlist = events if isinstance(events, list) else [events]
                    if not any(e.get('status') == 'connected' for e in evlist):
                        logger.warning('Massive WS options: unexpected greeting: %.200s', raw)

                    # 2. Authenticate
                    await ws.send(json.dumps({'action': 'auth', 'params': self.api_key}))
                    raw    = await asyncio.wait_for(ws.recv(), timeout=10)
                    events = json.loads(raw)
                    evlist = events if isinstance(events, list) else [events]
                    if not any(e.get('status') == 'auth_success' for e in evlist):
                        logger.error('Massive WS options auth failed: %.200s', raw)
                        return

                    # 3. Subscribe
                    await ws.send(json.dumps({'action': 'subscribe',
                                              'params': ','.join(self.channels)}))
                    logger.info('Massive WS options connected — subscribed to %s', self.channels)

                    # 4. Message loop
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            events = json.loads(raw)
                            if not isinstance(events, list):
                                events = [events]
                            for event in events:
                                ev_type = event.get('ev', '')
                                sym     = event.get('sym') or event.get('T', '')
                                await self.on_message(ev_type, sym, event)
                        except Exception as exc:
                            logger.warning('Massive WS message error: %s', exc)

            except Exception as exc:
                if not self._running:
                    break
                logger.warning('Massive WS options disconnected: %s — retry in %ds', exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        logger.info('Massive WS options stopped')

    async def disconnect(self):
        self._running = False


# ---------------------------------------------------------------------------
# MassiveOptionsCapture — options OA.* → Redis unusual flow cache
# ---------------------------------------------------------------------------

class MassiveOptionsCapture:
    """
    Streams per-minute options aggregates (OA.*), accumulates intraday volume
    per contract, and writes unusual-flow signals to Redis.

    Redis keys: massive:flow:{underlying}  (TTL 4h)
    Value: JSON {unusual_call_flow, unusual_put_flow, call_vol, put_vol,
                  call_oi, put_oi, updated_at}

    Consumed by fetch_polygon_flow() in pipeline.py — eliminates per-ticker
    REST polling for strategies that use options flow signals.
    """

    UNUSUAL_FLOW_THRESHOLD = 0.30   # session volume > 30% of prev-close OI → unusual
    REDIS_TTL_SECONDS      = 14400  # 4-hour key TTL

    def __init__(self, api_key: str = None, realtime: bool = True):
        self.api_key   = api_key
        self.realtime  = realtime
        # session_vol[underlying][contract_type] = total_volume_today
        self._session_vol: Dict[str, Dict[str, float]] = defaultdict(lambda: {'call': 0.0, 'put': 0.0})
        # prev_oi[underlying][contract_type] = total_open_interest (from last EOD)
        self._prev_oi:     Dict[str, Dict[str, float]] = defaultdict(lambda: {'call': 0.0, 'put': 0.0})
        self._redis        = None
        self._client:      Optional[MassiveWSClient] = None

    async def _load_prev_oi(self):
        """Load previous-close OI from options_eod.parquet."""
        from pathlib import Path
        try:
            import pandas as pd
            path = (Path(__file__).resolve().parent.parent.parent
                    / 'data' / 'master' / 'options_eod.parquet')
            if not path.exists():
                logger.warning('MassiveOptionsCapture: options_eod.parquet not found '
                               '— flow threshold comparisons disabled')
                return
            df = pd.read_parquet(path)
            df['_dt'] = pd.to_datetime(df['date'])
            latest = df['_dt'].max()
            df = df[df['_dt'] == latest]

            oi_col   = next((c for c in df.columns if 'open_interest' in c.lower()), None)
            type_col = next((c for c in df.columns if 'type' in c.lower()), None)
            und_col  = next((c for c in ('ticker', 'underlying', 'symbol') if c in df.columns), None)

            if not all([oi_col, type_col, und_col]):
                logger.warning('MassiveOptionsCapture: options_eod columns not recognised '
                               '(found: %s)', list(df.columns)[:10])
                return

            for _, row in df.iterrows():
                und  = str(row[und_col]).upper()
                typ  = str(row[type_col]).lower()
                oi   = float(row[oi_col] or 0)
                if typ in ('call', 'put'):
                    self._prev_oi[und][typ] += oi

            logger.info('MassiveOptionsCapture: loaded OI for %d underlyings from %s',
                        len(self._prev_oi), latest.date())
        except Exception as exc:
            logger.warning('MassiveOptionsCapture: OI load error: %s', exc)

    async def _on_message(self, ev_type: str, sym: str, event: dict):
        if ev_type != 'OA' or not sym:
            return
        parsed = _parse_occ(sym)
        if not parsed:
            return
        underlying, _expiry, opt_type, _strike = parsed

        vol = float(event.get('v') or 0)
        if vol > 0:
            self._session_vol[underlying][opt_type] += vol
            await self._update_flow_cache(underlying)

    async def _update_flow_cache(self, underlying: str):
        call_vol = self._session_vol[underlying]['call']
        put_vol  = self._session_vol[underlying]['put']
        call_oi  = self._prev_oi[underlying]['call']
        put_oi   = self._prev_oi[underlying]['put']

        unusual_call = call_oi > 0 and call_vol > self.UNUSUAL_FLOW_THRESHOLD * call_oi
        unusual_put  = put_oi  > 0 and put_vol  > self.UNUSUAL_FLOW_THRESHOLD * put_oi

        payload = json.dumps({
            'unusual_call_flow': unusual_call,
            'unusual_put_flow':  unusual_put,
            'call_vol':          call_vol,
            'put_vol':           put_vol,
            'call_oi':           call_oi,
            'put_oi':            put_oi,
            'updated_at':        datetime.utcnow().isoformat(),
        })
        if self._redis:
            await self._redis.set(f'massive:flow:{underlying}', payload,
                                  ex=self.REDIS_TTL_SECONDS)

    async def run(self):
        await self._load_prev_oi()
        import redis.asyncio as aioredis
        self._redis  = aioredis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'))
        self._client = MassiveWSClient(['OA.*'], self._on_message, self.api_key, self.realtime)
        await self._client.connect()

    async def stop(self):
        if self._client:
            await self._client.disconnect()
        if self._redis:
            await self._redis.aclose()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    mode = sys.argv[1] if len(sys.argv) > 1 else 'run'

    if mode in ('run', 'all', 'options'):
        logger.info('Starting MassiveOptionsCapture (options OA.* feed)...')
        asyncio.run(MassiveOptionsCapture().run())

    elif mode == 'test':
        count = 0
        async def _opt_handler(ev, sym, event):
            global count
            if ev == 'OA':
                count += 1
                parsed = _parse_occ(sym)
                print(f'OA {sym} → {parsed}  v={event.get("v")}')
                if count >= 5:
                    raise KeyboardInterrupt
            else:
                print(f'[{ev}] {sym} {event}')
        async def _test():
            c = MassiveWSClient(['OA.*'], _opt_handler)
            try:
                await asyncio.wait_for(c.connect(), timeout=120)
            except (asyncio.TimeoutError, KeyboardInterrupt):
                pass
        asyncio.run(_test())

    else:
        print('Usage: python massive_ws.py [run|test]')
