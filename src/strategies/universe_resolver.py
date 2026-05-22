from __future__ import annotations
from dataclasses import dataclass
from datetime import date as _date
from typing import Callable, Iterable, Protocol
import importlib

from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import DEFAULT_UNIVERSE_FILTER


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
        self._cache: dict[tuple[str, _date], list[str]] = {}
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
        if as_of > self._today_fn():
            raise AsOfInFutureError(f"as_of {as_of} > today {self._today_fn()}")
        key = (strategy_id, as_of)
        if key in self._cache:
            return self._cache[key]
        predicate = self._load_predicate(strategy_id)
        rows = self._db.fetch_metadata_as_of(as_of)
        out = []
        for row in rows:
            meta = row.metadata if hasattr(row, "metadata") else TickerMetadata.from_row(row)
            try:
                if predicate(meta, as_of) and self._coverage.has_floor(meta.symbol, as_of):
                    out.append(meta.symbol)
            except Exception:
                # Defensive: a broken predicate skips the ticker; lifecycle
                # sandbox check should have caught this earlier.
                continue
        out.sort()
        self._cache[key] = out
        return out

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
