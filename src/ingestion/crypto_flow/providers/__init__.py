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
    from src.ingestion.crypto_flow.providers.deribit_vol import DeribitVol
    from src.ingestion.crypto_flow.providers.hl_whales import HlWhales
    from src.ingestion.crypto_flow.providers.xrp_onchain import XrpOnchain
    from src.ingestion.crypto_flow.providers.sol_onchain import SolOnchain
    from src.ingestion.crypto_flow.providers.utxo_alts import UtxoAlts
    from src.ingestion.crypto_flow.providers.cftc_cot import CftcCot
    from src.ingestion.crypto_flow.providers.stables_defi import StablesDefi
    from src.ingestion.crypto_flow.providers.macro_sent import MacroSent
    from src.ingestion.crypto_flow.providers.santiment import Santiment
    return [BtcOnchain(), EvmOnchain(), Derivs(), Liquidations(), SpotTape(), DeribitVol(), HlWhales(),
            XrpOnchain(), SolOnchain(), UtxoAlts(),
            CftcCot(), StablesDefi(), MacroSent(), Santiment()]
