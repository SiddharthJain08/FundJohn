"""
Massive flat-file client — options data only (options starter plan).

Bucket layout (flatfiles) — options feeds accessible on this plan:
  us_options_opra/day_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz   ~4.0 MB/day  full OPRA chain

Stock price data comes from Yahoo Finance (yfinance) or FMP — not Massive/Polygon.
"""

import gzip
import io
import logging
import os
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

MASSIVE_ENDPOINT = os.environ.get('MASSIVE_S3_ENDPOINT', 'https://files.massive.com')
MASSIVE_BUCKET   = os.environ.get('MASSIVE_S3_BUCKET', 'flatfiles')

OPTIONS_COLS = ['ticker', 'volume', 'vwap', 'open', 'close', 'high', 'low', 'transactions', 'date',
                'underlying_ticker', 'expiration_date', 'strike_price', 'contract_type']


def _s3_client():
    """Build boto3 S3 client with Massive credentials."""
    try:
        import boto3
        from botocore.client import Config
        import urllib3
        urllib3.disable_warnings()
        return boto3.client(
            's3',
            endpoint_url=MASSIVE_ENDPOINT,
            aws_access_key_id=os.environ.get('MASSIVE_ACCESS_KEY_ID', ''),
            aws_secret_access_key=os.environ.get('MASSIVE_SECRET_KEY', ''),
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
            verify=False,
        )
    except ImportError:
        return None


def list_available_dates(feed: str = 'us_options_opra', year: int = None) -> list[str]:
    """
    List available dates in a Massive feed.
    Works today (list-only S3 access).

    Args:
        feed: one of us_stocks_sip, us_options_opra, us_indices, global_crypto, global_forex,
              us_futures_cme, us_futures_cbot, us_futures_comex, us_futures_nymex
        year: filter to a specific year (default: current year)
    Returns:
        Sorted list of 'YYYY-MM-DD' date strings.
    """
    s3 = _s3_client()
    if s3 is None:
        return []
    if year is None:
        year = date.today().year
    prefix = f'{feed}/day_aggs_v1/{year}/'
    dates = []
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=MASSIVE_BUCKET, Prefix=prefix):
            for obj in page.get('Contents') or []:
                key = obj['Key']
                fname = key.rsplit('/', 1)[-1]
                if fname.endswith('.csv.gz') and len(fname) == 17:
                    dates.append(fname[:-7])
        return sorted(dates)
    except Exception as e:
        logger.warning('Massive list_available_dates error: %s', e)
        return []


def _download_s3(feed: str, trade_date: str) -> Optional[bytes]:
    """
    Attempt to download a flat file via S3.
    Returns raw gzipped bytes or None if access is denied or unavailable.
    """
    s3 = _s3_client()
    if s3 is None:
        return None
    year, month = trade_date[:4], trade_date[5:7]
    key = f'{feed}/day_aggs_v1/{year}/{month}/{trade_date}.csv.gz'
    try:
        obj = s3.get_object(Bucket=MASSIVE_BUCKET, Key=key)
        return obj['Body'].read()
    except Exception as e:
        err_str = str(e)
        if '403' in err_str or 'Forbidden' in err_str:
            logger.debug('Massive S3 GetObject 403 for %s/%s — falling back to REST API', feed, trade_date)
        else:
            logger.warning('Massive S3 download error for %s: %s', key, e)
        return None


def download_options_day_bars(trade_date: str = None) -> pd.DataFrame:
    """
    Download full OPRA options EOD bars for a given date.

    Tries Massive S3 flat file first (full chain, ~4MB/day).
    Falls back to per-ticker Polygon options snapshot for a limited universe
    (Polygon REST doesn't support bulk options dump in one call).

    Args:
        trade_date: 'YYYY-MM-DD'; defaults to the most recent trading day.
    Returns:
        DataFrame with options EOD data. Columns depend on source.
        S3 path includes: ticker, underlying_ticker, expiration_date, strike_price,
                          contract_type, open, high, low, close, volume, vwap, transactions
    """
    if trade_date is None:
        trade_date = _last_trading_day()

    raw = _download_s3('us_options_opra', trade_date)
    if raw:
        df = _parse_csv_gz(raw, trade_date, 'options')
        logger.info('download_options_day_bars: %d contracts via Massive S3 for %s', len(df), trade_date)
        return df

    logger.info('download_options_day_bars: S3 unavailable for %s — Polygon per-ticker fallback required', trade_date)
    return pd.DataFrame()  # Polygon REST has no single-call bulk options dump


def download_index_day_bars(trade_date: str = None) -> pd.DataFrame:
    """Download all US index EOD values for a given date."""
    if trade_date is None:
        trade_date = _last_trading_day()

    raw = _download_s3('us_indices', trade_date)
    if raw:
        return _parse_csv_gz(raw, trade_date, 'indices')
    return pd.DataFrame()


def _parse_csv_gz(raw_bytes: bytes, trade_date: str, kind: str) -> pd.DataFrame:
    """Parse gzipped CSV flat file bytes into a DataFrame."""
    try:
        text = gzip.decompress(raw_bytes).decode('utf-8')
        df   = pd.read_csv(io.StringIO(text))
        df.columns = [c.lower().strip() for c in df.columns]
        # Normalize ticker column (may be named 'ticker', 'symbol', or 'T')
        for col_name in ('ticker', 'symbol', 't'):
            if col_name in df.columns and 'ticker' not in df.columns:
                df.rename(columns={col_name: 'ticker'}, inplace=True)
                break
        if 'ticker' in df.columns:
            df['ticker'] = df['ticker'].str.upper()
        df['date'] = trade_date
        return df
    except Exception as e:
        logger.warning('Massive _parse_csv_gz error (%s): %s', kind, e)
        return pd.DataFrame()


def _last_trading_day() -> str:
    """Return the most recent weekday date as 'YYYY-MM-DD'."""
    d = date.today()
    if d.weekday() == 6:   # Sunday
        d -= timedelta(days=2)
    elif d.weekday() == 5:  # Saturday
        d -= timedelta(days=1)
    return d.isoformat()


def probe_access() -> dict:
    """
    Test Massive S3 options access with current credentials.
    Returns a status dict useful for diagnostics.
    """
    status = {'list_objects': False, 'get_object': False}

    try:
        s3 = _s3_client()
        if s3:
            resp = s3.list_objects_v2(Bucket=MASSIVE_BUCKET,
                                      Prefix='us_options_opra/day_aggs_v1/', MaxKeys=1)
            status['list_objects'] = bool(resp.get('KeyCount', 0) or resp.get('Contents'))
    except Exception:
        pass

    dates = list_available_dates('us_options_opra')
    test_date = dates[-1] if dates else _last_trading_day()
    raw = _download_s3('us_options_opra', test_date)
    status['get_object'] = raw is not None

    logger.info('Massive probe: %s', status)
    return status
