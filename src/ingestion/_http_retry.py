"""
_http_retry.py — shared retry helper for ingestion fetchers.

3-attempt exponential backoff on 429/5xx/transient URLErrors. Returns
response bytes (or raises after final attempt). Logs each retry.
"""

import socket
import sys
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def fetch_with_retry(
    url_or_req,
    *,
    timeout: int = 30,
    attempts: int = 3,
    base_delay: float = 2.0,
    label: str = 'http',
) -> Optional[bytes]:
    """
    Fetch via urllib, retrying on transient failures.

    Returns response bytes on success, None on final failure (caller handles).
    Exponential backoff: base_delay, base_delay*2, base_delay*4.
    Honors Retry-After header on 429.

    Catches HTTPError, URLError, and socket-level timeouts (TimeoutError /
    socket.timeout / OSError). Pre-2026-05-02 a socket read timeout from
    arxiv slipped past as an uncaught TimeoutError, killing the whole
    sweep — that's the regression that dropped 2026-05-02 saturday-brain
    ingestion from 656 → 31 papers.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(url_or_req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as e:
            last_exc = e
            retryable = e.code in RETRYABLE_STATUS
            if not retryable or attempt == attempts:
                print(f'[{label}] HTTP {e.code} (final): {url_of(url_or_req)[:140]}', file=sys.stderr)
                return None
            delay = _retry_after(e) or base_delay * (2 ** (attempt - 1))
            print(f'[{label}] HTTP {e.code} attempt {attempt}/{attempts}; sleeping {delay:.1f}s', file=sys.stderr)
            time.sleep(delay)
        except URLError as e:
            last_exc = e
            if attempt == attempts:
                print(f'[{label}] URLError (final): {e}', file=sys.stderr)
                return None
            delay = base_delay * (2 ** (attempt - 1))
            print(f'[{label}] URLError attempt {attempt}/{attempts}: {e}; sleeping {delay:.1f}s', file=sys.stderr)
            time.sleep(delay)
        except (TimeoutError, socket.timeout) as e:
            last_exc = e
            if attempt == attempts:
                print(f'[{label}] timeout (final, {timeout}s): {url_of(url_or_req)[:140]}', file=sys.stderr)
                return None
            delay = base_delay * (2 ** (attempt - 1))
            print(f'[{label}] timeout attempt {attempt}/{attempts} ({timeout}s); sleeping {delay:.1f}s', file=sys.stderr)
            time.sleep(delay)
        except OSError as e:
            # Catch-all for transient network/socket failures (DNS, conn-reset,
            # SSL handshake errors). Same retry policy as URLError.
            last_exc = e
            if attempt == attempts:
                print(f'[{label}] OSError (final): {type(e).__name__}: {e}', file=sys.stderr)
                return None
            delay = base_delay * (2 ** (attempt - 1))
            print(f'[{label}] OSError attempt {attempt}/{attempts}: {type(e).__name__}: {e}; sleeping {delay:.1f}s', file=sys.stderr)
            time.sleep(delay)
    return None


def _retry_after(e: HTTPError) -> Optional[float]:
    val = e.headers.get('Retry-After') if getattr(e, 'headers', None) else None
    if not val:
        return None
    try:
        return max(1.0, float(val))
    except (TypeError, ValueError):
        return None


def url_of(url_or_req) -> str:
    if isinstance(url_or_req, Request):
        return url_or_req.full_url
    return str(url_or_req)
