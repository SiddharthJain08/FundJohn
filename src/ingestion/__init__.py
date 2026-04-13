"""
OpenClaw data ingestion package.
Async 3-layer pipeline: Fetch → Transform → Cache.
Schedule: daily 4:20 PM EST (market close + 20-min cooldown).
"""

from .pipeline import (
    MasterBar,
    run_pipeline,
    fetch_fmp_earnings,
    fetch_fmp_insider,
    fetch_fmp_prices,
    fetch_massive_chain,
    fetch_massive_flow,
    transform_to_masterbar,
    cache_write,
    cache_read,
    start_scheduler,
)

__all__ = [
    'MasterBar',
    'run_pipeline',
    'fetch_fmp_earnings',
    'fetch_fmp_insider',
    'fetch_fmp_prices',
    'fetch_massive_chain',
    'fetch_massive_flow',
    'transform_to_masterbar',
    'cache_write',
    'cache_read',
    'start_scheduler',
]
