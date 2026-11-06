"""Crypto-flow data providers."""
from __future__ import annotations

from src.ingestion.crypto_flow.providers.base import Provider


def all_providers() -> list[Provider]:
    # Tasks 4-10 append their provider classes here, in fallback-preference order.
    return []
