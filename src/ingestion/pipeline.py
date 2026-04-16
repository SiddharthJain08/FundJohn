"""
OpenClaw Ingestion Pipeline — 3-layer async ETL.

Layer 1 — Fetch:  aiohttp with semaphore rate-limiting
  FMP Starter:    300 req/min  → semaphore(5 concurrent)
  Massive Starter: 60 req/min → semaphore(2 concurrent)

Layer 2 — Transform: normalize raw API responses into MasterBar,
  compute EMA20, EMA50, RSI(14) from OHLCV history.

Layer 3 — Cache: data/cache/{symbol}/{date}.json, TTL 23h.

Schedule: daily 16:20 America/New_York via APScheduler.
"""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / 'data' / 'cache'
CACHE_TTL_SECONDS = 23 * 3600  # 23 hours

FMP_BASE   = 'https://financialmodelingprep.com/api/v3'
MASSIVE_BASE = 'https://api.massivetrader.com/v1'   # placeholder — swap for real host

FMP_CONCURRENCY    = 5   # semaphore slots for FMP (300 req/min → safe at 5 parallel)
MASSIVE_CONCURRENCY = 2  # semaphore slots for Massive (60 req/min → safe at 2 parallel)


# ---------------------------------------------------------------------------
# MasterBar schema
# ---------------------------------------------------------------------------

@dataclass
class MasterBar:
    symbol:               str
    timestamp:            str            # ISO date 'YYYY-MM-DD'

    # OHLCV
    open:                 Optional[float] = None
    high:                 Optional[float] = None
    low:                  Optional[float] = None
    close:                Optional[float] = None
    volume:               Optional[float] = None

    # Options
    iv_rank:              Optional[float] = None
    open_interest_by_strike: Dict[float, float] = field(default_factory=dict)
    expiry_date:          Optional[str]   = None

    # Flow
    unusual_call_flow:    bool = False
    unusual_put_flow:     bool = False

    # Fundamentals
    earnings_surprise_pct: Optional[float] = None
    insider_buy_flag:      bool = False
    insider_sell_flag:     bool = False

    # Computed indicators
    ema_20:               Optional[float] = None
    ema_50:               Optional[float] = None
    rsi_14:               Optional[float] = None


# ---------------------------------------------------------------------------
# Layer 3 — Cache
# ---------------------------------------------------------------------------

def _cache_path(symbol: str, date: str) -> Path:
    return CACHE_DIR / symbol / f'{date}.json'


def cache_write(bar: MasterBar) -> None:
    """Serialise MasterBar to data/cache/{symbol}/{date}.json."""
    path = _cache_path(bar.symbol, bar.timestamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'written_at': time.time(),
        'bar': asdict(bar),
    }
    path.write_text(json.dumps(payload, default=str), encoding='utf-8')


def cache_read(symbol: str, date: str) -> Optional[MasterBar]:
    """
    Return cached MasterBar if it exists and is within TTL, else None.
    TTL is 23 hours from write time.
    """
    path = _cache_path(symbol, date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        age = time.time() - float(payload.get('written_at', 0))
        if age > CACHE_TTL_SECONDS:
            return None
        bar_dict = payload['bar']
        # Restore Dict[float, float] keys (JSON keys are always strings)
        raw_oi = bar_dict.get('open_interest_by_strike') or {}
        bar_dict['open_interest_by_strike'] = {float(k): float(v) for k, v in raw_oi.items()}
        return MasterBar(**bar_dict)
    except Exception:
        logger.warning('Cache read failed for %s/%s', symbol, date)
        return None


# ---------------------------------------------------------------------------
# Layer 2 — Transform / indicators
# ---------------------------------------------------------------------------

def _ema(prices: List[float], period: int) -> Optional[float]:
    """Exponential moving average — returns the last value or None."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 6)


def _rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Wilder-smoothed RSI.  prices should be close values oldest-first."""
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains   = [max(c, 0) for c in changes]
    losses  = [abs(min(c, 0)) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 4)


def transform_to_masterbar(
    symbol:    str,
    date:      str,
    ohlcv:     List[Dict],            # [{date, open, high, low, close, volume}, ...]
    chain:     Optional[Dict] = None, # {iv_rank, open_interest_by_strike, expiry_date}
    flow:      Optional[Dict] = None, # {unusual_call_flow, unusual_put_flow}
    earnings:  Optional[Dict] = None, # {earnings_surprise_pct}
    insider:   Optional[Dict] = None, # {insider_buy_flag, insider_sell_flag}
) -> MasterBar:
    """
    Assemble all fetched data into a MasterBar and compute indicators.
    ohlcv list must be sorted oldest-first.
    """
    bar = MasterBar(symbol=symbol, timestamp=date)

    # OHLCV — last bar is today's
    if ohlcv:
        latest = ohlcv[-1]
        bar.open   = _safe_float(latest.get('open'))
        bar.high   = _safe_float(latest.get('high'))
        bar.low    = _safe_float(latest.get('low'))
        bar.close  = _safe_float(latest.get('close'))
        bar.volume = _safe_float(latest.get('volume'))

        closes = [_safe_float(b.get('close')) for b in ohlcv if b.get('close') is not None]
        bar.ema_20 = _ema(closes, 20)
        bar.ema_50 = _ema(closes, 50)
        bar.rsi_14 = _rsi(closes, 14)

    # Options chain
    if chain:
        bar.iv_rank  = _safe_float(chain.get('iv_rank'))
        bar.expiry_date = chain.get('expiry_date')
        raw_oi = chain.get('open_interest_by_strike') or {}
        bar.open_interest_by_strike = {float(k): float(v) for k, v in raw_oi.items()}

    # Unusual flow
    if flow:
        bar.unusual_call_flow = bool(flow.get('unusual_call_flow', False))
        bar.unusual_put_flow  = bool(flow.get('unusual_put_flow', False))

    # Earnings
    if earnings:
        bar.earnings_surprise_pct = _safe_float(earnings.get('earnings_surprise_pct'))

    # Insider
    if insider:
        bar.insider_buy_flag  = bool(insider.get('insider_buy_flag', False))
        bar.insider_sell_flag = bool(insider.get('insider_sell_flag', False))

    return bar


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Layer 1 — Fetch (async)
# ---------------------------------------------------------------------------

_fmp_semaphore:    Optional[asyncio.Semaphore] = None
_massive_semaphore: Optional[asyncio.Semaphore] = None


def _get_fmp_sem() -> asyncio.Semaphore:
    global _fmp_semaphore
    if _fmp_semaphore is None:
        _fmp_semaphore = asyncio.Semaphore(FMP_CONCURRENCY)
    return _fmp_semaphore


def _get_massive_sem() -> asyncio.Semaphore:
    global _massive_semaphore
    if _massive_semaphore is None:
        _massive_semaphore = asyncio.Semaphore(MASSIVE_CONCURRENCY)
    return _massive_semaphore


async def fetch_fmp_earnings(
    session: aiohttp.ClientSession,
    symbol:  str,
    api_key: str,
) -> Optional[Dict]:
    """
    GET /earnings-surprises/{symbol}
    Returns {'earnings_surprise_pct': float} from the most recent quarter, or None.
    """
    url = f'{FMP_BASE}/earnings-surprises/{symbol}'
    async with _get_fmp_sem():
        try:
            async with session.get(url, params={'apikey': api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning('FMP earnings %s: HTTP %s', symbol, resp.status)
                    return None
                data = await resp.json(content_type=None)
                if not isinstance(data, list) or not data:
                    return None
                latest = data[0]
                actual   = _safe_float(latest.get('actualEarningResult'))
                estimate = _safe_float(latest.get('estimatedEarning'))
                if estimate is None or estimate == 0:
                    pct = None
                else:
                    pct = round((actual - estimate) / abs(estimate) * 100, 4) if actual is not None else None
                return {'earnings_surprise_pct': pct}
        except Exception as e:
            logger.warning('FMP earnings %s error: %s', symbol, e)
            return None


async def fetch_fmp_insider(
    session: aiohttp.ClientSession,
    symbol:  str,
    api_key: str,
    lookback_days: int = 30,
) -> Optional[Dict]:
    """
    GET /insider-trading?symbol={symbol}
    Returns {'insider_buy_flag': bool, 'insider_sell_flag': bool}.
    Looks at transactions within the last lookback_days calendar days.
    """
    url = f'{FMP_BASE}/insider-trading'
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    async with _get_fmp_sem():
        try:
            async with session.get(
                url,
                params={'symbol': symbol, 'apikey': api_key, 'limit': 50},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning('FMP insider %s: HTTP %s', symbol, resp.status)
                    return None
                data = await resp.json(content_type=None)
                if not isinstance(data, list):
                    return None

                buy_flag = sell_flag = False
                for txn in data:
                    try:
                        txn_date = datetime.strptime(str(txn.get('transactionDate', ''))[:10], '%Y-%m-%d')
                    except ValueError:
                        continue
                    if txn_date < cutoff:
                        continue
                    txn_type = (txn.get('transactionType') or '').upper()
                    if 'BUY' in txn_type or 'PURCHASE' in txn_type:
                        buy_flag = True
                    elif 'SALE' in txn_type or 'SELL' in txn_type:
                        sell_flag = True

                return {'insider_buy_flag': buy_flag, 'insider_sell_flag': sell_flag}
        except Exception as e:
            logger.warning('FMP insider %s error: %s', symbol, e)
            return None


async def fetch_fmp_prices(
    session:     aiohttp.ClientSession,
    symbol:      str,
    api_key:     str,
    lookback:    int = 60,
) -> Optional[List[Dict]]:
    """
    GET /historical-price-full/{symbol}
    Returns list of [{date, open, high, low, close, volume}] sorted oldest-first.
    lookback controls how many calendar days of history to request.
    """
    url = f'{FMP_BASE}/historical-price-full/{symbol}'
    from_date = (datetime.utcnow() - timedelta(days=lookback)).strftime('%Y-%m-%d')
    async with _get_fmp_sem():
        try:
            async with session.get(
                url,
                params={'from': from_date, 'apikey': api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning('FMP prices %s: HTTP %s', symbol, resp.status)
                    return None
                data = await resp.json(content_type=None)
                historical = data.get('historical') if isinstance(data, dict) else None
                if not historical:
                    return None
                # API returns newest-first; reverse to oldest-first
                bars = []
                for h in reversed(historical):
                    bars.append({
                        'date':   h.get('date'),
                        'open':   _safe_float(h.get('open')),
                        'high':   _safe_float(h.get('high')),
                        'low':    _safe_float(h.get('low')),
                        'close':  _safe_float(h.get('close')),
                        'volume': _safe_float(h.get('volume')),
                    })
                return bars
        except Exception as e:
            logger.warning('FMP prices %s error: %s', symbol, e)
            return None


async def fetch_massive_chain(
    session: aiohttp.ClientSession,
    symbol:  str,
    api_key: str,
) -> Optional[Dict]:
    """
    GET /chain/{symbol}
    Returns {iv_rank, open_interest_by_strike: {strike: oi}, expiry_date} or None.
    """
    url = f'{MASSIVE_BASE}/chain/{symbol}'
    async with _get_massive_sem():
        try:
            async with session.get(
                url,
                headers={'X-API-Key': api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning('Massive chain %s: HTTP %s', symbol, resp.status)
                    return None
                data = await resp.json(content_type=None)

                # Normalise OI: expect list of {strike, call_oi, put_oi} or {strike, oi}
                raw_chain = data.get('chain') or data.get('contracts') or []
                oi_dict: Dict[float, float] = {}
                for contract in raw_chain:
                    strike = _safe_float(contract.get('strike') or contract.get('strikePrice'))
                    if strike is None:
                        continue
                    oi = _safe_float(contract.get('oi') or contract.get('openInterest'))
                    call_oi = _safe_float(contract.get('call_oi') or contract.get('callOI'))
                    put_oi  = _safe_float(contract.get('put_oi')  or contract.get('putOI'))
                    total = (oi or 0) + (call_oi or 0) + (put_oi or 0)
                    if total > 0:
                        oi_dict[strike] = total

                # Nearest expiry — try top-level field
                expiry = (
                    data.get('expiry_date')
                    or data.get('expiryDate')
                    or data.get('expiration')
                )

                return {
                    'iv_rank':                _safe_float(data.get('iv_rank') or data.get('ivRank')),
                    'open_interest_by_strike': oi_dict,
                    'expiry_date':             expiry,
                }
        except Exception as e:
            logger.warning('Massive chain %s error: %s', symbol, e)
            return None


async def fetch_massive_flow(
    session: aiohttp.ClientSession,
    symbol:  str,
    api_key: str,
) -> Optional[Dict]:
    """
    GET /flow/unusual/{symbol}
    Returns {unusual_call_flow: bool, unusual_put_flow: bool} or None.
    """
    url = f'{MASSIVE_BASE}/flow/unusual/{symbol}'
    async with _get_massive_sem():
        try:
            async with session.get(
                url,
                headers={'X-API-Key': api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning('Massive flow %s: HTTP %s', symbol, resp.status)
                    return None
                data = await resp.json(content_type=None)
                return {
                    'unusual_call_flow': bool(
                        data.get('unusual_call_flow')
                        or data.get('unusualCallFlow')
                        or data.get('call_unusual')
                    ),
                    'unusual_put_flow': bool(
                        data.get('unusual_put_flow')
                        or data.get('unusualPutFlow')
                        or data.get('put_unusual')
                    ),
                }
        except Exception as e:
            logger.warning('Massive flow %s error: %s', symbol, e)
            return None


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

async def _process_symbol(
    session:     aiohttp.ClientSession,
    symbol:      str,
    date:        str,
    fmp_key:     str,
    massive_key: str,
    force:       bool = False,
) -> Optional[MasterBar]:
    """Fetch, transform, cache one symbol. Returns MasterBar or None."""
    if not force:
        cached = cache_read(symbol, date)
        if cached is not None:
            logger.debug('%s: cache hit', symbol)
            return cached

    # Kick off all fetches in parallel
    prices_task  = asyncio.create_task(fetch_fmp_prices(session, symbol, fmp_key))
    earnings_task = asyncio.create_task(fetch_fmp_earnings(session, symbol, fmp_key))
    insider_task  = asyncio.create_task(fetch_fmp_insider(session, symbol, fmp_key))
    chain_task    = asyncio.create_task(fetch_massive_chain(session, symbol, massive_key))
    flow_task     = asyncio.create_task(fetch_massive_flow(session, symbol, massive_key))

    ohlcv, earnings, insider, chain, flow = await asyncio.gather(
        prices_task, earnings_task, insider_task, chain_task, flow_task,
        return_exceptions=False,
    )

    bar = transform_to_masterbar(
        symbol   = symbol,
        date     = date,
        ohlcv    = ohlcv or [],
        chain    = chain,
        flow     = flow,
        earnings = earnings,
        insider  = insider,
    )

    cache_write(bar)
    logger.info('%s: ingested (close=%.2f, ema20=%.2f, rsi=%.1f)',
                symbol,
                bar.close or 0,
                bar.ema_20 or 0,
                bar.rsi_14 or 0)
    return bar



async def fetch_fmp_earnings_calendar(
    session:  aiohttp.ClientSession,
    fmp_key:  str,
    from_date: str,
    to_date:   str,
) -> list:
    """
    Fetch upcoming earnings calendar from FMP for a date window.
    GET /earning_calendar?from=YYYY-MM-DD&to=YYYY-MM-DD
    Returns list of dicts: {symbol, date, eps, epsEstimated, revenue, revenueEstimated}
    Called daily by pipeline_orchestrator to keep earnings.parquet current.
    """
    url = f'{FMP_BASE}/earning_calendar'
    params = {"from": from_date, "to": to_date, "apikey": fmp_key}
    async with _fmp_semaphore:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                logger.warning("FMP earning_calendar HTTP %s", resp.status)
        except Exception as e:
            logger.warning("FMP earning_calendar error: %s", e)
    return []

async def run_pipeline(
    symbols:    List[str],
    date:       Optional[str] = None,
    fmp_key:    Optional[str] = None,
    massive_key: Optional[str] = None,
    force:      bool = False,
) -> List[MasterBar]:
    """
    Run the full ingestion pipeline for a list of symbols.

    Args:
        symbols:    Ticker list, e.g. ['AAPL', 'MSFT']
        date:       ISO date string 'YYYY-MM-DD'; defaults to today (UTC)
        fmp_key:    FMP API key; falls back to $FMP_API_KEY env var
        massive_key: Massive API key; falls back to $MASSIVE_API_KEY env var
        force:      Bypass cache even if fresh

    Returns:
        List of MasterBar objects (one per symbol, may be partial if fetches fail)
    """
    if date is None:
        date = datetime.utcnow().strftime('%Y-%m-%d')

    fmp_key     = fmp_key     or os.environ.get('FMP_API_KEY', '')
    massive_key = massive_key or os.environ.get('MASSIVE_API_KEY', '')

    # Reset semaphores on each run (handles reuse across event loops in tests)
    global _fmp_semaphore, _massive_semaphore
    _fmp_semaphore    = asyncio.Semaphore(FMP_CONCURRENCY)
    _massive_semaphore = asyncio.Semaphore(MASSIVE_CONCURRENCY)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _process_symbol(session, sym, date, fmp_key, massive_key, force)
            for sym in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    bars = []
    for sym, result in zip(symbols, results):
        if isinstance(result, Exception):
            logger.error('%s: pipeline error: %s', sym, result)
        elif result is not None:
            bars.append(result)

    logger.info('Pipeline complete: %d/%d symbols ingested for %s', len(bars), len(symbols), date)
    return bars


# ---------------------------------------------------------------------------
# Scheduler — 4:20 PM America/New_York daily
# ---------------------------------------------------------------------------

def start_scheduler(symbols: List[str]) -> None:
    """
    Start APScheduler to run the pipeline daily at 16:20 America/New_York.
    Blocks — call from main entrypoint.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone='America/New_York')

    def job():
        logger.info('Scheduler: starting pipeline run')
        asyncio.run(run_pipeline(symbols))

    scheduler.add_job(
        job,
        trigger='cron',
        hour=16,
        minute=20,
        id='daily_ingestion',
        name='Market-close data ingestion',
        misfire_grace_time=300,  # 5-minute grace if cron fires late
    )

    logger.info('Scheduler armed: daily 16:20 America/New_York')
    scheduler.start()
