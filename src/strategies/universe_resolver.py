from __future__ import annotations
from dataclasses import dataclass
from datetime import date as _date
from typing import Callable, Iterable, Protocol
import importlib
import logging
import os

from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import DEFAULT_UNIVERSE_FILTER, tier_liquid

logger = logging.getLogger(__name__)


def _liquid_cap_enabled() -> bool:
    """Operator ruling 2026-09-04: tier_liquid is the LARGEST universe any
    strategy may resolve — live, union, envelope, or backtest. A predicate
    wider than the ladder's top tier (no_otc matched 12,124 names including
    SPAC units/warrants via S_ast_roa_effect_within_stocks) is intersected
    down to it, so there is no path to going live — or backtesting — on
    names outside tier_liquid. Kill switch: OPENCLAW_UNIVERSE_LIQUID_CAP=0."""
    return os.environ.get('OPENCLAW_UNIVERSE_LIQUID_CAP', '1') != '0'


class AsOfInFutureError(ValueError):
    """Raised when resolve(as_of) > today() — defense against look-ahead bias."""


class _DBProtocol(Protocol):
    def fetch_metadata_as_of(self, as_of: _date) -> list: ...

class _CoverageProtocol(Protocol):
    def has_floor(self, symbol: str, as_of: _date) -> bool: ...


class UniverseResolver:
    def __init__(self, db: _DBProtocol, coverage: _CoverageProtocol,
                 manifest_loader: Callable[[], dict] | None = None,
                 today_fn: Callable[[], _date] = _date.today,
                 audit_writer: Callable[[dict], None] | None = None):
        self._db = db
        self._coverage = coverage
        self._cache: dict[tuple[str, _date, bool], list[str]] = {}
        self._manifest_loader = manifest_loader
        self._today_fn = today_fn
        self._audit_writer = audit_writer

    def _load_predicate(self, strategy_id: str) -> Callable[[TickerMetadata, _date], bool]:
        if self._manifest_loader is None:
            return DEFAULT_UNIVERSE_FILTER
        manifest = self._manifest_loader()
        ref = (manifest.get("strategies", {}).get(strategy_id, {})
                       .get("metadata", {}).get("universe_filter_ref"))
        if ref is None:
            return DEFAULT_UNIVERSE_FILTER
        mod_path, attr = ref.rsplit(":", 1)
        module = importlib.import_module(mod_path)
        return getattr(module, attr)

    def resolve(self, strategy_id: str, as_of: _date) -> list[str]:
        return self._resolve(strategy_id, as_of, apply_floor=True)

    def _resolve(self, strategy_id: str, as_of: _date, apply_floor: bool = True) -> list[str]:
        if as_of > self._today_fn():
            raise AsOfInFutureError(f"as_of {as_of} > today {self._today_fn()}")
        key = (strategy_id, as_of, apply_floor)
        if key in self._cache:
            return self._cache[key]
        predicate = self._load_predicate(strategy_id)
        rows = self._db.fetch_metadata_as_of(as_of)
        cap_on = _liquid_cap_enabled()
        capped = 0
        out = []
        for row in rows:
            meta = row.metadata if hasattr(row, "metadata") else TickerMetadata.from_row(row)
            try:
                if not predicate(meta, as_of):
                    continue
                if cap_on and not tier_liquid(meta, as_of):
                    capped += 1
                    continue
                if apply_floor and not self._coverage.has_floor(meta.symbol, as_of):
                    continue
                out.append(meta.symbol)
            except Exception:
                # Defensive: a broken predicate skips the ticker; lifecycle
                # sandbox check should have caught this earlier.
                continue
        if capped:
            logger.info('universe_resolver: liquid cap dropped %d name(s) for %s @ %s '
                        '(predicate wider than tier_liquid)', capped, strategy_id, as_of)
        out.sort()
        self._cache[key] = out
        return out

    def envelope_universe(self, as_of: _date, states: tuple[str, ...] = ("live",)) -> list[str]:
        """SP-7 Phase C C2 fetch envelope: predicate-only union, NO coverage
        floor (spec §4). The floor gates strategy resolve; the fetch envelope
        must include newly adopted tiers so their data accrues — otherwise the
        coverage-floor chicken-and-egg never dies."""
        seen: set[str] = set()
        for sid in self._live_strategy_ids(states):
            seen.update(self._resolve(sid, as_of, apply_floor=False))
        return sorted(seen)

    def _live_strategy_ids(self, states: tuple[str, ...]) -> list[str]:
        if self._manifest_loader is None:
            return []
        manifest = self._manifest_loader()
        return [sid for sid, rec in manifest.get("strategies", {}).items()
                if rec.get("state") in states]

    def _alpaca_universe_size(self) -> int:
        # Overridden in production with a DB count; default 0 for tests
        return 0

    def union_universe(self, as_of: _date, states: tuple[str, ...] = ("live",)) -> list[str]:
        import time
        t0 = time.monotonic()
        per_strategy: dict[str, int] = {}
        seen: set[str] = set()
        for sid in self._live_strategy_ids(states):
            tickers = self.resolve(sid, as_of)
            per_strategy[sid] = len(tickers)
            seen.update(tickers)
        union = sorted(seen)
        if self._audit_writer:
            self._audit_writer({
                "resolved_for_date": as_of,
                "lifecycle_states": list(states),
                "union_size": len(union),
                "per_strategy_sizes": per_strategy,
                "alpaca_universe_size": self._alpaca_universe_size(),
                "resolver_ms": int((time.monotonic() - t0) * 1000),
            })
        return union


class MockResolver(UniverseResolver):
    """SP-2 Phase C grid helper: force a candidate predicate, bypassing the
    manifest-registered one. Reuses resolve()'s db-fetch / coverage-floor /
    look-ahead-guard / sort / cache unchanged."""

    def __init__(self, db: _DBProtocol, coverage: _CoverageProtocol,
                 predicate: Callable[[object, _date], bool], **kw):
        super().__init__(db=db, coverage=coverage, **kw)
        self._forced_predicate = predicate

    def _load_predicate(self, strategy_id: str) -> Callable[[object, _date], bool]:
        return self._forced_predicate


if __name__ == "__main__":
    import argparse, json, os, sys
    from datetime import date as _d
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--states", default="live")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strategy", help="Resolve a single strategy instead of union")
    ap.add_argument("--envelope", action="store_true",
                    help="No-floor fetch envelope (SP-7 C2) instead of the floored union")
    args = ap.parse_args()
    as_of = _d.fromisoformat(args.as_of)
    states = tuple(args.states.split(","))
    db = PostgresMetadataDB(os.environ["POSTGRES_URI"])
    cov = CoverageIndex.from_parquet("/root/openclaw/data/master/prices.parquet")
    def manifest_loader():
        with open("/root/openclaw/src/strategies/manifest.json") as f:
            return json.load(f)
    resolver = UniverseResolver(db=db, coverage=cov, manifest_loader=manifest_loader)
    if args.strategy:
        out = resolver.resolve(args.strategy, as_of=as_of)
    elif args.envelope:
        out = resolver.envelope_universe(as_of=as_of, states=states)
    else:
        out = resolver.union_universe(as_of=as_of, states=states)
    print(json.dumps(out))
