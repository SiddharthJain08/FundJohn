"""Crypto flow universe: the Alpaca crypto pairs and their venue / on-chain identifiers.

One row per Alpaca active ``*/USD`` pair in ``crypto_flow_universe.json``
(venue symbols, chain, Etherscan V2 chainid, ERC-20 / SPL contract, CoinGecko id).
Three spellings of a pair exist in the system and this module owns the mapping:

    ticker   'BTC-USD'  (internal, matches crypto_bars_1h)
    alpaca   'BTC/USD'  (asset list / orders)
    position 'BTCUSD'   (broker positions)

APPEND-ONLY (CLAUDE.md core invariant): a pair that leaves Alpaca's list is
flagged ``active=False``, never removed. Refresh via
``scripts/refresh_crypto_flow_universe.py``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

UNIVERSE_PATH = Path(__file__).resolve().parent / "crypto_flow_universe.json"


@dataclass(frozen=True)
class Asset:
    ticker: str
    alpaca: str
    position: str
    role: str = "alt"  # 'benchmark' | 'alt' | 'stable' | 'commodity'
    active: bool = True
    chain: str | None = None
    evm_chainid: int | None = None
    token_contract: str | None = None  # ERC-20 on evm_chainid / SPL mint; None for native
    decimals: int | None = None
    binance: str | None = None
    hyperliquid: str | None = None
    bybit: str | None = None
    okx: str | None = None
    coinbase: str | None = None
    coingecko_id: str | None = None


_FIELDS = {f.name for f in fields(Asset)}


def load_universe(path: Path = UNIVERSE_PATH, *, active_only: bool = True) -> list[Asset]:
    """Rows in file order; ``active_only`` drops ``active: false`` rows."""
    raw = json.loads(Path(path).read_text())
    assets = [Asset(**{k: v for k, v in row.items() if k in _FIELDS}) for row in raw["assets"]]
    return [a for a in assets if a.active] if active_only else assets


def by_ticker(assets: list[Asset]) -> dict[str, Asset]:
    return {a.ticker: a for a in assets}


_ALL_CACHE: list[Asset] | None = None


def normalize_broker_symbol(sym: str, assets: list[Asset] | None = None) -> str | None:
    """'BTCUSD' | 'BTC/USD' | 'BTC-USD' -> 'BTC-USD'; non-crypto symbols -> None.

    Matches inactive rows too (a held position can outlive the listing).
    """
    global _ALL_CACHE
    key = (sym or "").replace("/", "").replace("-", "").upper()
    if not key.endswith("USD"):
        return None
    if assets is None:
        if _ALL_CACHE is None:
            _ALL_CACHE = load_universe(active_only=False)
        assets = _ALL_CACHE
    for a in assets:
        if a.position == key:
            return a.ticker
    return None


def internal_symbol(sym, *, crypto: bool = False) -> str:
    """Any broker / engine spelling -> the engine key. The ONE wrapper every
    execution module uses (Phase 3 D-1) around :func:`normalize_broker_symbol`.

    * a known pair in any form ('BTCUSD' | 'BTC/USD' | 'BTC-USD') -> 'BTC-USD';
    * an unknown slash pair ('XYZ/USD') -> 'XYZ-USD' (the legacy '/'->'-' rule);
    * an unknown compact pair ('XYZUSD') -> 'XYZ-USD' ONLY when the caller knows
      the row is crypto (``crypto=True``, e.g. broker ``asset_class == 'crypto'``);
    * anything else (equities) -> stripped + upper-cased, otherwise unchanged.

    Never raises: a missing/corrupt universe file degrades to the shape rules
    (the reconcile / breaker paths are safety paths)."""
    s = str(sym or '').strip().upper()
    try:
        n = normalize_broker_symbol(s)
    except Exception:  # noqa: BLE001 - universe file missing/corrupt
        n = None
    if n:
        return n
    if '/' in s:
        return s.replace('/', '-')
    if crypto and len(s) > 3 and s.endswith('USD') and '-' not in s:
        return s[:-3] + '-USD'
    return s


def is_crypto_symbol(sym) -> bool:
    """Shape + universe test: True for any crypto spelling ('/' form, '-USD'
    internal form, or a compact 'BTCUSD' the universe knows). Never raises."""
    s = str(sym or '').strip().upper()
    if '/' in s or s.endswith('-USD'):
        return True
    try:
        return normalize_broker_symbol(s) is not None
    except Exception:  # noqa: BLE001
        return False


def refresh_from_alpaca(
    existing: list[Asset], alpaca_symbols: list[str]
) -> tuple[list[Asset], list[str], list[str]]:
    """Merge Alpaca's live ``BASE/USD`` list into ``existing``.

    Returns (merged, added_tickers, deactivated_tickers). Never removes a row:
    unknown pairs are appended (role 'alt', venue fields None), absent pairs are
    flagged inactive, and a previously inactive pair that reappears is re-activated.
    """
    live = {s.replace("/", "-").upper() for s in alpaca_symbols if s.upper().endswith("/USD")}
    known = {a.ticker for a in existing}
    merged: list[Asset] = []
    deactivated: list[str] = []
    for a in existing:
        if a.ticker in live and not a.active:
            a = replace(a, active=True)
        elif a.ticker not in live and a.active:
            a = replace(a, active=False)
            deactivated.append(a.ticker)
        merged.append(a)
    added: list[str] = []
    for t in sorted(live - known):
        base = t[: -len("-USD")]
        merged.append(Asset(ticker=t, alpaca=f"{base}/USD", position=f"{base}USD"))
        added.append(t)
    return merged, added, deactivated
