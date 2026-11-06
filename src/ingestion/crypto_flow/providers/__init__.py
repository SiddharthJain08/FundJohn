"""Crypto-flow data providers."""
from __future__ import annotations

from src.ingestion.crypto_flow.providers.base import Provider


def all_providers() -> list[Provider]:
    # Tasks 4-10 append their provider classes here, in fallback-preference order.
    from src.ingestion.crypto_flow.providers.btc_onchain import BtcOnchain
    from src.ingestion.crypto_flow.providers.evm_onchain import EvmOnchain
    from src.ingestion.crypto_flow.providers.derivs import Derivs
    from src.ingestion.crypto_flow.providers.liquidations import Liquidations
    from src.ingestion.crypto_flow.providers.spot_tape import SpotTape
    return [BtcOnchain(), EvmOnchain(), Derivs(), Liquidations(), SpotTape()]
