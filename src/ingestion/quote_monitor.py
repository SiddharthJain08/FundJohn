"""quote_monitor — Phase 2D unified quote orchestrator.

Concept-lifted from achannarasappa/ticker's ``internal/monitor/monitor.go``:
a partition-by-source fan-out with per-source acknowledgement deadline.
First non-stale price per ticker wins.

This module is IMPORT-SAFE under default config — the orchestrator is never
invoked unless the operator explicitly calls :func:`fetch_quotes` or runs the
parity-capture entrypoint with ``OPENCLAW_UNIFIED_QUOTES=1``. The daily
``collect`` step in ``src/execution/pipeline_orchestrator.py`` is untouched.

Design (engineer-judgment notes per spec line 759):
  * **Primary source** for parity-diff = Polygon. Matches today's live
    production path; gives a stable ``source_a`` slot in
    ``quote_monitor_parity``. The alternative (symmetric recency arbitration)
    was rejected: it doubles the orchestrator's complexity for no win during
    the 5-day parity-observation period.
  * Per-source 3s acknowledgement timeout via ``asyncio.wait_for``. A source
    that doesn't return within 3s is treated as "no data this fan-out" —
    it does NOT cancel the other sources.
  * Source failure isolation: an adapter raising any Exception leaves the
    other adapters' returns intact. The orchestrator surfaces the failure
    via ``FanOutResult.errors[source]``.
  * Staleness: each :class:`Quote` carries ``fetched_at`` (UTC, monotonic).
    A quote whose ``age_sec`` exceeds ``stale_after_sec`` is dropped from
    the winner-selection logic but still recorded for parity-diff inspection.
  * Dedup-on-rapid-update: if :meth:`QuoteMonitor.set_symbols` is called
    twice within ``dedup_window_sec`` for the same ticker set, the second
    call is a no-op and returns the cached :class:`FanOutResult`.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Optional, Protocol, runtime_checkable

from src.ingestion import quote_sources as _registry


# Default-OFF feature gate. Unset (or anything other than '1') means: the
# orchestrator is importable but the collect step never calls it.
ENV_GATE = 'OPENCLAW_UNIFIED_QUOTES'

# Per-source ack timeout. Spec D2.4: "3s per-source ack timeout".
DEFAULT_ACK_TIMEOUT_SEC = 3.0

# After this many seconds, a quote is "stale" and dropped from
# winner-selection (but still appears in parity-diff output).
DEFAULT_STALE_AFTER_SEC = 60.0

# Repeat set_symbols([...]) within this window returns the cached result.
DEFAULT_DEDUP_WINDOW_SEC = 0.5


# ── Protocol (D2.2) ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    """A point-in-time quote for a single ticker from a single source.

    ``fetched_at`` is a UTC datetime (NOT a monotonic clock). ``age_sec`` is
    computed against ``time.time()`` at read-out so the dataclass stays
    serialisable to the parity table.
    """
    ticker: str
    price: float
    source: str
    fetched_at: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[float] = None
    raw: Optional[dict] = None

    @property
    def age_sec(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.fetched_at).total_seconds())

    @property
    def is_stale(self) -> bool:
        return self.age_sec > DEFAULT_STALE_AFTER_SEC


@runtime_checkable
class QuoteSource(Protocol):
    """Adapter contract. Each concrete adapter MUST be async."""

    name: str
    priority: int  # lower = higher priority (Polygon=0 wins ties)

    async def fetch(self, tickers: Iterable[str]) -> dict[str, Quote]:
        """Return ``{ticker: Quote}`` for whichever subset of ``tickers``
        this source could resolve. Missing tickers are silently dropped —
        the orchestrator handles fallback to other sources."""
        ...


# ── FanOutResult ────────────────────────────────────────────────────────────

@dataclass
class FanOutResult:
    """Per-symbol-set fan-out output.

    ``winners`` is the deduped {ticker: Quote} after applying staleness +
    priority. ``by_source`` retains every responding source's full payload
    so the parity-capture entrypoint can compute pairwise divergence
    without re-fetching.
    """
    winners: dict[str, Quote] = field(default_factory=dict)
    by_source: dict[str, dict[str, Quote]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # source -> exc text
    timed_out: set[str] = field(default_factory=set)      # sources that exceeded ack timeout
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Orchestrator (D2.4) ─────────────────────────────────────────────────────

class QuoteMonitor:
    """Multi-source quote fan-out with per-source ack timeout.

    Sources are added via :meth:`add_source` or pulled from
    ``src.ingestion.quote_sources.SOURCE_REGISTRY`` at construction time.
    """

    def __init__(
        self,
        ack_timeout_sec: float = DEFAULT_ACK_TIMEOUT_SEC,
        stale_after_sec: float = DEFAULT_STALE_AFTER_SEC,
        dedup_window_sec: float = DEFAULT_DEDUP_WINDOW_SEC,
        sources: Optional[list[QuoteSource]] = None,
    ):
        self.ack_timeout_sec = ack_timeout_sec
        self.stale_after_sec = stale_after_sec
        self.dedup_window_sec = dedup_window_sec
        self._sources: list[QuoteSource] = list(sources or [])
        self._symbols: tuple[str, ...] = ()
        self._last_set_at: float = 0.0
        self._last_result: Optional[FanOutResult] = None

    # ── source management ───────────────────────────────────────────────
    def add_source(self, source: QuoteSource) -> None:
        if not isinstance(source, QuoteSource):  # runtime_checkable Protocol
            raise TypeError(f'{source!r} does not implement QuoteSource')
        self._sources.append(source)

    def sources(self) -> list[QuoteSource]:
        return list(self._sources)

    # ── public API ──────────────────────────────────────────────────────
    def set_symbols(self, tickers: Iterable[str]) -> tuple[str, ...]:
        """Register the ticker set for the next fan-out.

        Dedup-on-rapid-update: if called with the same set of tickers within
        ``dedup_window_sec`` of the prior call AND the prior call has a
        cached result, returns the prior tuple unchanged (caller can pull
        the cached result via :attr:`last_result`)."""
        tickers_t = tuple(sorted({t.upper() for t in tickers if t}))
        now = time.time()
        if (
            tickers_t == self._symbols
            and self._last_result is not None
            and (now - self._last_set_at) < self.dedup_window_sec
        ):
            return self._symbols
        self._symbols = tickers_t
        self._last_set_at = now
        self._last_result = None
        return self._symbols

    @property
    def last_result(self) -> Optional[FanOutResult]:
        return self._last_result

    async def fan_out(self) -> FanOutResult:
        """Run all sources in parallel with per-source 3s timeout. Returns
        the merged FanOutResult; also stored as :attr:`last_result`."""
        result = FanOutResult()
        if not self._symbols:
            self._last_result = result
            return result
        if not self._sources:
            self._last_result = result
            return result

        async def _bounded(src: QuoteSource) -> tuple[str, object]:
            try:
                quotes = await asyncio.wait_for(
                    src.fetch(self._symbols), timeout=self.ack_timeout_sec,
                )
                return (src.name, quotes)
            except asyncio.TimeoutError:
                return (src.name, _TIMEOUT_SENTINEL)
            except Exception as e:  # source failure isolation
                return (src.name, e)

        gathered = await asyncio.gather(
            *(_bounded(s) for s in self._sources),
            return_exceptions=False,
        )

        for name, payload in gathered:
            if payload is _TIMEOUT_SENTINEL:
                result.timed_out.add(name)
                continue
            if isinstance(payload, Exception):
                result.errors[name] = f'{type(payload).__name__}: {payload}'
                continue
            assert isinstance(payload, dict)
            result.by_source[name] = payload

        result.winners = self._select_winners(result.by_source)
        self._last_result = result
        return result

    # ── winner selection ────────────────────────────────────────────────
    def _select_winners(
        self, by_source: dict[str, dict[str, Quote]],
    ) -> dict[str, Quote]:
        """First non-stale price per ticker wins. Ties broken by source
        priority (lower priority int = higher rank); Polygon's default
        priority is 0 so it wins ties under the production config."""
        # Build {ticker: [(priority, Quote), ...]}
        candidates: dict[str, list[tuple[int, Quote]]] = {}
        priority_map = {s.name: getattr(s, 'priority', 100) for s in self._sources}
        for src_name, quotes in by_source.items():
            for ticker, q in quotes.items():
                if q.age_sec > self.stale_after_sec:
                    continue  # stale — drop from winner selection
                candidates.setdefault(ticker, []).append(
                    (priority_map.get(src_name, 100), q),
                )
        winners: dict[str, Quote] = {}
        for ticker, lst in candidates.items():
            lst.sort(key=lambda pq: pq[0])  # lowest priority int first
            winners[ticker] = lst[0][1]
        return winners


# Sentinel value distinguishable from None / Exception / dict in _bounded().
_TIMEOUT_SENTINEL = object()


# ── convenience: gate-aware top-level entry point ───────────────────────────

def gate_enabled() -> bool:
    """``True`` iff ``OPENCLAW_UNIFIED_QUOTES=1`` in env. Default OFF."""
    return os.environ.get(ENV_GATE, '0') == '1'


async def fetch_quotes(
    tickers: Iterable[str],
    sources: Optional[list[QuoteSource]] = None,
    ack_timeout_sec: float = DEFAULT_ACK_TIMEOUT_SEC,
) -> FanOutResult:
    """One-shot helper: build a monitor, set symbols, fan out, return.

    Does NOT consult the env gate — caller is responsible. The gate exists
    so the daily ``collect`` step doesn't accidentally invoke this; parity
    capture + ad-hoc operator use both bypass the gate explicitly.
    """
    monitor = QuoteMonitor(ack_timeout_sec=ack_timeout_sec, sources=sources)
    monitor.set_symbols(tickers)
    return await monitor.fan_out()
