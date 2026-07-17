# SP-3 Asset-Class Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a strategy-level `instrument_class` (equity/option/etp; crypto/futures reserved) and thread it through lifecycle, sizer, backtest, and promotion guards, then prove the rails with one reference ETP strategy.

**Architecture:** A new top-level `instrument_class` manifest field on `StrategyRecord` (default `equity`, always-emitted), read by a sizing dispatcher, a class-aware backtest loader, and a per-class promotion-threshold table. Equity behavior is byte-identical; a default-OFF kill-switch (`OPENCLAW_INSTRUMENT_CLASS_ROUTING`) forces every strategy down the equity path until soak. Crypto/24-7 work is deferred to SP-3.1.

**Tech Stack:** Python 3 (pytest), `src/strategies/lifecycle.py`, `src/execution/regime_blended_sizer*.py`, `src/backtest/unified_backtest.py`, JSON manifest, Postgres (no migration in MVP), Alpaca CLI (`/root/go/bin/alpaca`).

**Spec:** `docs/superpowers/specs/2026-05-26-sp3-asset-class-expansion-design.md` (grounding table in §2 of the spec — every name below is verified against `main` b83e930).

**Standing constraints (every task):** never `git add -A` (stage by name); never delete from master parquets / canonical tables (append-only); surface before any merge/deploy (live VPS); the equity path must stay numerically identical. Tests run from repo root: `cd /root/openclaw && python3 -m pytest <path> -v` (tests insert `ROOT` and `ROOT/src` on `sys.path` themselves — see existing tests).

---

## File Structure

**Create:**
- `tests/test_lifecycle_instrument_class.py` — silent-strip + backfill + validation regression (Task 1/2)
- `src/strategies/instrument_class.py` — enum constants + `instrument_class_for(strategy_id)` resolver (Task 3)
- `tests/test_instrument_class_resolver.py` — resolver tests (Task 3)
- `src/execution/instrument_class_sizer.py` — sizing dispatcher (Task 5)
- `tests/test_instrument_class_sizer.py` — dispatcher tests (Task 5)
- `tests/test_promotion_thresholds.py` — per-class threshold tests (Task 4)
- `tests/test_instrument_class_backtest.py` — class-aware loader/cost-model tests (Task 6)
- `tests/fixtures/synthetic_etp_strategy.py` — synthetic fixture strategy, `tests/`-only (Task 7)
- `tests/test_instrument_class_end_to_end.py` — full-path smoke via the fixture (Task 7)
- `src/strategies/implementations/s_commodity_etp_momentum.py` — reference ETP strategy (Task 9)
- `tests/test_kill_switch_instrument_class.py` — kill-switch parity (Task 10)

**Modify:**
- `src/strategies/lifecycle.py` — `StrategyRecord` (:110), `from_manifest` (:286 + :303), `to_dict` (:677), `can_transition` (:424), threshold constants (:88-89) (Tasks 1,2,4)
- `src/execution/regime_blended_sizer_live.py` — wire dispatcher at `_build_sized_payload` (:39) (Task 5)
- `src/backtest/unified_backtest.py` — class-aware loader/cost-model in `run_backtest` (:572) (Task 6)
- `src/strategies/registry.py` — `_IMPL_MAP` (:17) add reference strategy (Task 9)
- `src/strategies/manifest.json` — reference strategy entry with `instrument_class` (Task 9)
- `.env.example` — document `OPENCLAW_INSTRUMENT_CLASS_ROUTING` (Task 10)

---

## BUCKET A — Field + threading (lands inert)

### Task 1: Add `instrument_class` to StrategyRecord + serialization (failing test first)

**Files:**
- Test: `tests/test_lifecycle_instrument_class.py`
- Modify: `src/strategies/lifecycle.py`

- [ ] **Step 1: Write the failing regression test** (template: `tests/test_lifecycle_eligible_regimes_preservation.py`)

```python
"""Regression: lifecycle.py must (a) round-trip top-level instrument_class,
(b) backfill absent instrument_class to 'equity', (c) never strip another
strategy's instrument_class during an unrelated promotion.

Run: pytest tests/test_lifecycle_instrument_class.py -v
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from strategies.lifecycle import LifecycleStateMachine  # noqa: E402


def _write(tmp_path, payload):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return p


def _entry(state='live', **extra):
    e = {'state': state, 'state_since': '2026-05-01T00:00:00Z',
         'metadata': {}, 'history': []}
    e.update(extra)
    return e


def test_declared_instrument_class_round_trips(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='etp')}})
    out = LifecycleStateMachine.from_manifest(p).to_dict()
    assert out['strategies']['s1']['instrument_class'] == 'etp'


def test_absent_instrument_class_backfills_equity(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry()}})
    out = LifecycleStateMachine.from_manifest(p).to_dict()
    assert out['strategies']['s1']['instrument_class'] == 'equity'


def test_save_manifest_preserves_instrument_class(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='etp')}})
    sm = LifecycleStateMachine.from_manifest(p)
    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['instrument_class'] == 'etp'


def test_unrelated_promotion_does_not_strip_instrument_class(tmp_path):
    p = _write(tmp_path, {'strategies': {
        's1': _entry(instrument_class='etp'),
        's2': _entry(state='staging'),
    }})
    sm = LifecycleStateMachine.from_manifest(p)
    sm.get_record('s2').state_since = '2026-05-12T19:25:00Z'
    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['instrument_class'] == 'etp', \
        'promotion of s2 must not strip s1.instrument_class'
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_lifecycle_instrument_class.py -v`
Expected: FAIL — `from_manifest` does not yet accept/emit `instrument_class` (TypeError on unexpected kwarg, or KeyError on the assert).

- [ ] **Step 3: Add the dataclass attribute**

In `src/strategies/lifecycle.py`, in `class StrategyRecord` (after `universe_filter_ref` at :127), add:

```python
    instrument_class: str = "equity"   # SP-3 — equity|option|etp (crypto|futures reserved)
```

- [ ] **Step 4: Thread through `from_manifest` (BOTH record constructions)**

In `from_manifest`, the main loop construction (:286) and the rescue construction (:303) — add this kwarg to each `StrategyRecord(...)` call:

```python
                instrument_class=rec.get("instrument_class", "equity"),
```

- [ ] **Step 5: Thread through `to_dict` (always-emit)**

In `to_dict`, inside the per-record loop after the `eligible_regimes` block (after :705, before `strategies[sid] = entry`), add:

```python
            # SP-3: always-emit (default 'equity') so legacy records backfill
            # on first write. Unlike eligible_regimes, this field is never omitted.
            entry["instrument_class"] = rec.instrument_class
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_lifecycle_instrument_class.py -v`
Expected: PASS (all 4).

- [ ] **Step 7: Run the full lifecycle suite (no regressions)**

Run: `cd /root/openclaw && python3 -m pytest tests/test_lifecycle_eligible_regimes_preservation.py tests/test_lifecycle_universe_filter_ref.py tests/test_lifecycle_regime_gate.py -v`
Expected: PASS (existing behavior intact).

- [ ] **Step 8: Commit**

```bash
git add src/strategies/lifecycle.py tests/test_lifecycle_instrument_class.py
git commit -m "feat(sp3): thread instrument_class through StrategyRecord (default equity, always-emit)"
```

### Task 2: Enum validation in `from_manifest`

**Files:**
- Modify: `src/strategies/lifecycle.py`
- Test: `tests/test_lifecycle_instrument_class.py`

- [ ] **Step 1: Add the failing validation test** (append to the test file)

```python
import pytest  # noqa: E402

def test_unknown_instrument_class_rejected(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='banana')}})
    with pytest.raises(ValueError, match='instrument_class'):
        LifecycleStateMachine.from_manifest(p)


def test_reserved_classes_accepted(tmp_path):
    for cls in ('crypto', 'futures'):
        p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class=cls)}})
        out = LifecycleStateMachine.from_manifest(p).to_dict()
        assert out['strategies']['s1']['instrument_class'] == cls
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_lifecycle_instrument_class.py -k instrument_class -v`
Expected: `test_unknown_instrument_class_rejected` FAILS (no validation yet).

- [ ] **Step 3: Add enum constants near the threshold constants (lifecycle.py ~:90)**

```python
# SP-3 instrument-class taxonomy. VALID = accepted by validation;
# ROUTED = has a live sizer/backtest handler in the MVP. crypto/futures
# are reserved (valid) but unhandled until SP-3.1.
VALID_INSTRUMENT_CLASSES  = frozenset({"equity", "option", "etp", "crypto", "futures"})
ROUTED_INSTRUMENT_CLASSES = frozenset({"equity", "option", "etp"})
```

- [ ] **Step 4: Validate in `from_manifest`** — in the main loop (:284-294), validate then use the validated local in the construction. The loop body becomes:

```python
        for sid, rec in data.get("strategies", {}).items():
            _ic = rec.get("instrument_class", "equity")
            if _ic not in VALID_INSTRUMENT_CLASSES:
                raise ValueError(
                    f"strategy {sid!r}: unknown instrument_class {_ic!r}; "
                    f"valid={sorted(VALID_INSTRUMENT_CLASSES)}")
            history = [TransitionEvent(**e) for e in rec.get("history", [])]
            records[sid] = StrategyRecord(
                strategy_id=sid,
                state=StrategyState(rec["state"]),
                state_since=rec["state_since"],
                history=history,
                metadata=rec.get("metadata", {}),
                eligible_regimes=rec.get("eligible_regimes"),
                universe_filter_ref=rec.get("metadata", {}).get("universe_filter_ref"),
                instrument_class=_ic,
            )
```

(The rescue construction at :303 from Task 1 Step 4 keeps `instrument_class=rec.get("instrument_class", "equity")` — rescued misrouted entries are tolerated without hard validation.)

- [ ] **Step 5: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_lifecycle_instrument_class.py -v`
Expected: PASS (all 6).

- [ ] **Step 6: Commit**

```bash
git add src/strategies/lifecycle.py tests/test_lifecycle_instrument_class.py
git commit -m "feat(sp3): validate instrument_class enum at manifest load"
```

### Task 3: `instrument_class_for(strategy_id)` resolver

**Files:**
- Create: `src/strategies/instrument_class.py`
- Test: `tests/test_instrument_class_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import json
from strategies.instrument_class import instrument_class_for, VALID_INSTRUMENT_CLASSES  # noqa


def _manifest(tmp_path, mapping):
    p = tmp_path / 'manifest.json'
    strategies = {sid: {'state': 'live', 'state_since': '2026-05-01T00:00:00Z',
                        'metadata': {}, 'history': [], 'instrument_class': ic}
                  for sid, ic in mapping.items()}
    p.write_text(json.dumps({'strategies': strategies}))
    return p


def test_resolves_declared_class(tmp_path):
    p = _manifest(tmp_path, {'s1': 'etp', 's2': 'equity'})
    assert instrument_class_for('s1', manifest_path=p) == 'etp'


def test_unknown_strategy_defaults_equity(tmp_path):
    p = _manifest(tmp_path, {'s1': 'etp'})
    assert instrument_class_for('nope', manifest_path=p) == 'equity'
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_resolver.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the resolver**

`src/strategies/instrument_class.py`:

```python
"""SP-3 instrument-class resolver. Reads the declared instrument_class for a
strategy from the manifest; defaults to 'equity' for unknown strategies.
Re-exports the taxonomy constants from lifecycle so callers have one import."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path

try:
    from .lifecycle import (VALID_INSTRUMENT_CLASSES, ROUTED_INSTRUMENT_CLASSES,
                            LifecycleStateMachine)
except ImportError:  # pragma: no cover - alt import root
    from strategies.lifecycle import (VALID_INSTRUMENT_CLASSES,  # type: ignore
                                      ROUTED_INSTRUMENT_CLASSES, LifecycleStateMachine)

_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.json"


def instrument_class_for(strategy_id: str, manifest_path: "str | Path | None" = None) -> str:
    """Return the declared instrument_class for strategy_id, or 'equity'."""
    path = Path(manifest_path) if manifest_path else _DEFAULT_MANIFEST
    sm = LifecycleStateMachine.from_manifest(path)
    rec = sm._records.get(strategy_id)
    return rec.instrument_class if rec is not None else "equity"
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/instrument_class.py tests/test_instrument_class_resolver.py
git commit -m "feat(sp3): instrument_class_for resolver (manifest-backed, equity default)"
```

---

## BUCKET B — Dispatcher, thresholds, class-aware backtest, fixture

### Task 4: Per-class promotion thresholds

**Files:**
- Modify: `src/strategies/lifecycle.py` (:88-89, :424)
- Test: `tests/test_promotion_thresholds.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import json
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa


def _sm(tmp_path, instrument_class):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'s1': {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': instrument_class}}}))
    return LifecycleStateMachine.from_manifest(p)


def test_equity_threshold_unchanged(tmp_path):
    sm = _sm(tmp_path, 'equity')
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.49, 'max_drawdown': 0.10})
    assert ok is False
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.51, 'max_drawdown': 0.10})
    assert ok is True


def test_etp_uses_its_own_row(tmp_path):
    # etp row equals equity in MVP; assert it is sourced from the table, not hardcoded
    sm = _sm(tmp_path, 'etp')
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.51, 'max_drawdown': 0.19})
    assert ok is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_promotion_thresholds.py -v`
Expected: `test_etp_uses_its_own_row` may pass coincidentally, but the table does not yet exist — the goal is the table-sourced lookup. Confirm both run; proceed.

- [ ] **Step 3: Add the threshold table (lifecycle.py, replacing the two scalars' role)**

Keep the existing constants (back-compat) and add the table just below them (~:92):

```python
# SP-3: per-instrument-class candidate→live thresholds. equity/etp keep the
# legacy values; option uses equity values as an explicit placeholder until
# SP-4 calibration. crypto added in SP-3.1.
PROMOTION_THRESHOLDS: dict[str, dict[str, float]] = {
    "equity": {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
               "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN},
    "etp":    {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
               "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN},
    "option": {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,   # TODO(SP-4): calibrate
               "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN},
}


def _promotion_threshold(instrument_class: str) -> dict[str, float]:
    return PROMOTION_THRESHOLDS.get(
        instrument_class,
        {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
         "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN})
```

- [ ] **Step 4: Wire `can_transition` to read the table (lifecycle.py :424-441)**

Replace the hardcoded comparisons in the candidate→live guard. After `drawdown = md.get("max_drawdown")` and the None-check, change:

```python
            thr = _promotion_threshold(rec.instrument_class)
            if sharpe < thr["min_sharpe"]:
                return False, (
                    f"candidate→live blocked: sharpe {sharpe:.2f} < "
                    f"minimum {thr['min_sharpe']} (instrument_class={rec.instrument_class})")
            if drawdown > thr["max_drawdown"]:
                return False, (
                    f"candidate→live blocked: max_drawdown {drawdown:.2%} > "
                    f"limit {thr['max_drawdown']:.0%} (instrument_class={rec.instrument_class})")
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_promotion_thresholds.py tests/test_lifecycle_regime_gate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/strategies/lifecycle.py tests/test_promotion_thresholds.py
git commit -m "feat(sp3): per-instrument-class promotion thresholds (equity byte-identical)"
```

### Task 5: Sizing dispatcher

**Files:**
- Create: `src/execution/instrument_class_sizer.py`
- Test: `tests/test_instrument_class_sizer.py`
- Modify: `src/execution/regime_blended_sizer_live.py` (:39 `_build_sized_payload`)

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest
from execution.instrument_class_sizer import apply_instrument_class_sizing  # noqa


def test_equity_is_identity():
    order = {'ticker': 'AAPL', 'notional_usd': 1000.0}
    out = apply_instrument_class_sizing(order, 'equity')
    assert out['notional_usd'] == 1000.0


def test_etp_is_identity():
    order = {'ticker': 'GLD', 'notional_usd': 1000.0}
    out = apply_instrument_class_sizing(order, 'etp')
    assert out['notional_usd'] == 1000.0


def test_option_scales_by_delta_when_present():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0, 'delta': 0.5}
    out = apply_instrument_class_sizing(order, 'option')
    assert out['notional_usd'] == 500.0


def test_option_without_delta_passes_through_failopen():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0}
    out = apply_instrument_class_sizing(order, 'option')
    assert out['notional_usd'] == 1000.0  # fail-open, no crash


def test_crypto_raises_no_handler():
    with pytest.raises(NotImplementedError, match='crypto'):
        apply_instrument_class_sizing({'ticker': 'BTC-USD', 'notional_usd': 1.0}, 'crypto')
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_sizer.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the dispatcher**

`src/execution/instrument_class_sizer.py`:

```python
"""SP-3 sizing dispatcher. Routes a single sized order by instrument_class.
equity/etp are literal pass-throughs (the live equity path must be byte-identical).
option scales notional by |delta| when a delta is carried on the order (fail-open
to raw notional otherwise — full greeks-driven delta sizing is an SP-3.x fast-follow).
crypto/futures raise (reserved, no handler until SP-3.1)."""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

_PASS_THROUGH = {"equity", "etp"}
_RESERVED = {"crypto", "futures"}


def apply_instrument_class_sizing(order: dict, instrument_class: str) -> dict:
    if instrument_class in _PASS_THROUGH:
        return order
    if instrument_class == "option":
        delta = order.get("delta")
        try:
            d = abs(float(delta)) if delta is not None else None
        except (TypeError, ValueError):
            d = None
        if d is None or d <= 0:
            logger.warning("[instrument_class_sizer] option order %s has no usable "
                           "delta; using raw notional (fail-open)", order.get("ticker"))
            return order
        scaled = dict(order)
        scaled["notional_usd"] = round(float(order["notional_usd"]) * d, 2)
        return scaled
    if instrument_class in _RESERVED:
        raise NotImplementedError(
            f"no sizer handler registered for instrument_class={instrument_class!r} "
            f"(reserved for SP-3.1)")
    raise NotImplementedError(f"unknown instrument_class={instrument_class!r}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_sizer.py -v`
Expected: PASS (5).

- [ ] **Step 5: Wire into the live sizer (kill-switch-gated)**

In `src/execution/regime_blended_sizer_live.py`, at the top of `_build_sized_payload` (:39), resolve routing once and apply per non-close order. Add imports near the existing imports (:33-36):

```python
import os
from strategies.instrument_class import instrument_class_for
from execution.instrument_class_sizer import apply_instrument_class_sizing
```

Inside the per-order loop, in the normal-bracket branch (after `notional = abs(float(o['notional_usd']))` at :121), insert BEFORE `pct_nav` is computed:

```python
        # SP-3: route by instrument_class. Default-OFF kill-switch forces the
        # equity path (byte-identical) until soak. No walrus — read into a local
        # so we don't shadow the `contributions` re-bound below at :135.
        if os.environ.get('OPENCLAW_INSTRUMENT_CLASS_ROUTING') == '1':
            _contribs = o.get('contributions') or []
            _sid_for_class = (_contribs[0].get('strategy_id')
                              if _contribs else o.get('strategy_id'))
            if _sid_for_class:
                _ic = instrument_class_for(_sid_for_class)
                if _ic not in ('equity', 'etp'):
                    o = apply_instrument_class_sizing(o, _ic)
                    notional = abs(float(o['notional_usd']))
```

(Note: equity/etp short-circuit means zero behavior change for all current strategies even when the gate is ON. Place this so `notional` used downstream reflects any option scaling. `apply_instrument_class_sizing` returns a new dict, so rebinding `o` updates the `notional_usd` read at :152.)

- [ ] **Step 6: Run the sizer's existing tests for no regression**

Run: `cd /root/openclaw && python3 -m pytest tests/ -k "sizer or sized_handoff or regime_blended" -v`
Expected: PASS (existing sizer behavior intact; gate is OFF by default in test env).

- [ ] **Step 7: Commit**

```bash
git add src/execution/instrument_class_sizer.py tests/test_instrument_class_sizer.py src/execution/regime_blended_sizer_live.py
git commit -m "feat(sp3): sizing dispatcher (equity/etp pass-through, option delta-scale, gated)"
```

### Task 6: Class-aware backtest loader + cost model

**Files:**
- Modify: `src/backtest/unified_backtest.py` (`run_backtest` :572)
- Test: `tests/test_instrument_class_backtest.py`

- [ ] **Step 1: Read first.** Read `src/backtest/unified_backtest.py:572-756` (`run_backtest`) to see the exact signature and where `load_prices_panels()` (:164) is called and where the result dict is assembled. The change is additive: accept an `instrument_class='equity'` kwarg, select the loader/cost-model, and record the class on the result.

- [ ] **Step 2: Write the failing test**

```python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest.unified_backtest import resolve_cost_model_bps, INSTRUMENT_COST_BPS  # noqa


def test_equity_and_etp_share_cost_model():
    assert resolve_cost_model_bps('equity') == resolve_cost_model_bps('etp')


def test_unknown_class_falls_back_to_equity_cost():
    assert resolve_cost_model_bps('weird') == resolve_cost_model_bps('equity')
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_backtest.py -v`
Expected: FAIL — symbol not defined.

- [ ] **Step 4: Add the cost-model table + resolver near the top of `unified_backtest.py`** (after `PRICES_PARQUET` :69)

```python
# SP-3: per-instrument-class execution cost (one-way, basis points). equity/etp
# share the equity model in MVP; options/crypto get their own when those engines land.
INSTRUMENT_COST_BPS: dict[str, float] = {"equity": 1.0, "etp": 1.0, "option": 5.0}


def resolve_cost_model_bps(instrument_class: str) -> float:
    return INSTRUMENT_COST_BPS.get(instrument_class, INSTRUMENT_COST_BPS["equity"])
```

(If `simulate_trade`/`_per_bar_simulate` already applies a commission/cost, wire `resolve_cost_model_bps` in as the bps source for the selected class; if it currently uses a hardcoded value, replace that read with `resolve_cost_model_bps(instrument_class)`. Confirm by reading `simulate_trade` :208.)

- [ ] **Step 5: Add `instrument_class` kwarg to `run_backtest`** (:572). To guarantee equity backtests stay byte-identical, MVP does **not** alter `simulate_trade`/`_per_bar_simulate` math and does **not** add a DB column (no migration). The cost model is *resolved and logged* now; wiring it into actual fills is a fast-follow.

  Change the signature (after `resolver=None` at :579):

```python
                 resolver=None,
                 instrument_class: str = 'equity') -> str:
```

  After `strategy_cls = load_strategy_class(filepath)` (~:597), add a log line that surfaces the resolved cost model (no behavioral effect):

```python
    _cost_bps = resolve_cost_model_bps(instrument_class)
    _log(f'instrument_class={instrument_class} cost_model_bps={_cost_bps}')
```

  Leave `load_prices_panels()` and the simulation untouched — all MVP classes (equity/etp) load the same panel and run identical math. This satisfies §3.3 "records which class it backtested" via the log without a schema change.

- [ ] **Step 6: Run to verify it passes + no regression**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_backtest.py tests/test_backtest_as_of.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_instrument_class_backtest.py
git commit -m "feat(sp3): class-aware backtest cost model + recorded instrument_class"
```

### Task 7: Synthetic fixture + full-path smoke

**Files:**
- Create: `tests/fixtures/synthetic_etp_strategy.py`, `tests/test_instrument_class_end_to_end.py`

- [ ] **Step 1: Write the end-to-end smoke test** (no production manifest mutation — uses a tmp manifest)

```python
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest
from strategies.lifecycle import LifecycleStateMachine, StrategyState
from strategies.instrument_class import instrument_class_for
from execution.instrument_class_sizer import apply_instrument_class_sizing


def _tmp_manifest(tmp_path, sid, ic):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {sid: {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': ic}}}))
    return p


def test_etp_full_path(tmp_path):
    p = _tmp_manifest(tmp_path, 'synthetic_etp', 'etp')
    assert instrument_class_for('synthetic_etp', manifest_path=p) == 'etp'
    order = {'ticker': 'GLD', 'notional_usd': 1000.0,
             'contributions': [{'strategy_id': 'synthetic_etp'}]}
    assert apply_instrument_class_sizing(order, 'etp')['notional_usd'] == 1000.0
    sm = LifecycleStateMachine.from_manifest(p)
    ok, _ = sm.can_transition('synthetic_etp', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.15})
    assert ok is True


def test_crypto_class_raises_through_dispatcher():
    with pytest.raises(NotImplementedError):
        apply_instrument_class_sizing({'ticker': 'BTC-USD', 'notional_usd': 1.0}, 'crypto')
```

- [ ] **Step 2: Create the fixture strategy** (`tests/fixtures/synthetic_etp_strategy.py`) — a minimal **`BaseStrategy`** subclass following the PRODUCTION strategy contract (read `src/strategies/implementations/s_earnings_news_specific_momentum.py:18-24` first for the real interface: subclass `BaseStrategy`, set `active_in_regimes` + `MAX_SIGNALS` class attrs, implement `generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]`). NOT `StrategyTemplate` (that ABC is the backtest-oracle clone, not the engine contract). The fixture returns one deterministic long `Signal` on a synthetic ticker. It exists ONLY for tests; never added to `manifest.json` or `_IMPL_MAP`. Add a smoke step in the test that instantiates it and asserts `generate_signals(...)` returns a non-empty list — proving the production contract without needing `prices.parquet`.

- [ ] **Step 3: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_instrument_class_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/synthetic_etp_strategy.py tests/test_instrument_class_end_to_end.py
git commit -m "test(sp3): synthetic ETP fixture + full-path smoke"
```

### Task 8: Executor pass-through awareness (guard test only)

**Files:**
- Test: `tests/test_instrument_class_sizer.py` (append) — assert ETP orders are treated as `us_equity` (no executor change needed).

- [ ] **Step 1:** Confirm by reading `src/execution/alpaca_executor.py:653-688`: the asset-class skip reads Alpaca's `class` field and ETPs return `us_equity`. No code change. Add a short comment in `instrument_class_sizer.py` documenting that ETP execution rides the equity path (no executor branch in MVP). Commit if a comment was added:

```bash
git add src/execution/instrument_class_sizer.py
git commit -m "docs(sp3): note ETP rides us_equity executor path (no MVP executor change)"
```

(If no change is warranted, skip the commit — record the verification in the task notes.)

---

## BUCKET C — ETP coverage + reference strategy

### Task 9: Reference ETP strategy + coverage

**Files:**
- Create: `src/strategies/implementations/s_commodity_etp_momentum.py`
- Modify: `src/strategies/registry.py` (:17 `_IMPL_MAP`), `src/strategies/manifest.json`

- [ ] **Step 1: Probe (G2) — confirm basket tradability.** First verify the exact subcommand/flags (do NOT assume `asset get`): run `/root/go/bin/alpaca asset --help` and `/root/go/bin/alpaca asset list --help`. Then export only the two keys (do NOT `source .env` — unquoted parens break bash):

```bash
export ALPACA_API_KEY=$(python3 -c "from dotenv import dotenv_values as d; print(d('/root/openclaw/.env')['ALPACA_API_KEY'])")
export ALPACA_API_SECRET=$(python3 -c "from dotenv import dotenv_values as d; v=d('/root/openclaw/.env'); print(v.get('ALPACA_API_SECRET') or v.get('ALPACA_SECRET_KEY'))")
# Use whichever real form --help confirms, e.g.:
#   /root/go/bin/alpaca asset list --status active | jq '.[] | select(.symbol=="GLD")'
# Confirm each of GLD SLV USO DBC returns class=us_equity, tradable=true.
unset ALPACA_API_KEY ALPACA_API_SECRET
```

Expected: each basket ticker is `class: us_equity, tradable: true`. Substitute a liquid alternative for any that fail. **Surface basket changes to the operator.**

- [ ] **Step 2: Ensure price coverage (append-only).** Add the 4 basket tickers to `universe_config` as `active=true` so the daily collector keeps them fresh (read `src/database/migrations/008_market_universe.sql` + `027_universe_sync.sql` for the table shape; use an `INSERT ... ON CONFLICT DO UPDATE SET active=true` — never DELETE). Then backfill history with the existing driver (read `scripts/backfill_universe_5y.py --help`-equivalent via `main` :886; it must NOT overwrite existing rows — `_promote_chunk` enforces the zero-existing precondition). Verify post-backfill: the basket tickers have ≥2y of rows in `prices.parquet`.

- [ ] **Step 3: Read the strategy interface.** Read 2 existing momentum implementations (`grep -ln "momentum" src/strategies/implementations/*.py | head -2`) and the `BaseStrategy` base they subclass. Learn the exact production contract: `BaseStrategy` subclass, `active_in_regimes` + `MAX_SIGNALS` class attrs, `generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]`, and how `Signal` brackets (entry/stop/target) are constructed. Match a template's structure exactly — do NOT use `StrategyTemplate`.

- [ ] **Step 4: Implement the strategy.** Signal rule (fully specified): on each `generate_signals` call, restrict to the basket tickers present in `universe`/`prices`, rank them by trailing 90-trading-day total return, emit ONE long `Signal` for the top-ranked ETP (flat if <90 days history), with a bracket constructed the SAME way the template strategy from Step 3 builds its bracket (reuse its ATR/stop/target idiom rather than inventing one). **Cadence:** do NOT hand-roll a calendar gate — production cadence is governed by the sharpe-cadence / `strategy_state` machinery for live strategies; for the backtest+candidate proof, match the template's cadence handling (likely none inside `generate_signals`). Keep the logic minimal and well-commented: the goal is to exercise the rails, not discover alpha.

- [ ] **Step 5: Register.** Add the `_IMPL_MAP` entry in `registry.py` (`'S_commodity_etp_momentum': ('strategies.implementations.s_commodity_etp_momentum', '<ClassName>')`) and a `manifest.json` entry with `state: 'candidate'`, `instrument_class: 'etp'`, `metadata.canonical_file`, `metadata.class`, and a basket-appropriate `metadata.universe_filter_ref` (or document that it carries its own basket). Match the entry shape of an existing candidate strategy.

- [ ] **Step 6: Backtest → candidate.** Run the class-aware backtest:

```bash
cd /root/openclaw && python3 -m backtest.unified_backtest --strategy S_commodity_etp_momentum
```

Expected: produces metrics incl. `instrument_class: 'etp'`. Record Sharpe/MaxDD. It stays `candidate` regardless — **do NOT promote to live** (operator-only, post-soak).

- [ ] **Step 7: Validate registry + manifest load.**

Run: `cd /root/openclaw && python3 -c "from src.strategies.registry import load_strategy_class; print(load_strategy_class('S_commodity_etp_momentum'))" && python3 -m pytest tests/test_lifecycle_instrument_class.py -v`
Expected: class loads; lifecycle tests still pass; full manifest still loads (no enum-validation error).

- [ ] **Step 8: Commit**

```bash
git add src/strategies/implementations/s_commodity_etp_momentum.py src/strategies/registry.py src/strategies/manifest.json
git commit -m "feat(sp3): reference commodity-ETP momentum strategy (instrument_class=etp, candidate)"
```

---

## BUCKET D — Kill-switch gate + soak + closeout

### Task 10: Kill-switch documentation + parity test

**Files:**
- Modify: `.env.example`
- Test: `tests/test_kill_switch_instrument_class.py`

- [ ] **Step 1: Write the load-bearing gate-parity test** — proves the spec's most important claim: with the gate OFF, an *option* order flows through `_build_sized_payload` with notional **unchanged** (equity path); with the gate ON, the dispatcher scales it. Monkeypatch the resolver so no production manifest entry is needed.

```python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import regime_blended_sizer_live as rbsl  # noqa


def _orders_handoff():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0, 'direction': 1, 'delta': 0.5,
             'bracket': {'entry_price': 100.0, 'stop_loss': 95.0, 'take_profit_1': 110.0},
             'contributions': [{'strategy_id': 'opt_strat', 'attribution_weight': 1.0}]}
    handoff = {'cycle_date': '2026-05-26', 'regime': {'state': 'LOW_VOL'}}
    return [order], handoff


def test_gate_off_leaves_option_notional_unchanged(monkeypatch):
    monkeypatch.delenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', raising=False)
    monkeypatch.setattr(rbsl, 'instrument_class_for', lambda *a, **k: 'option')
    orders, handoff = _orders_handoff()
    out = rbsl._build_sized_payload(orders, handoff, equity=100_000.0)
    assert out['orders'][0]['notional_usd'] == 1000.0  # equity path, unscaled


def test_gate_on_scales_option_notional(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    monkeypatch.setattr(rbsl, 'instrument_class_for', lambda *a, **k: 'option')
    orders, handoff = _orders_handoff()
    out = rbsl._build_sized_payload(orders, handoff, equity=100_000.0)
    assert out['orders'][0]['notional_usd'] == 500.0  # scaled by |delta|=0.5
```

(This also confirms `apply_instrument_class_sizing` is only invoked under the gate. Task 5's unit tests + Task 7's smoke cover the dispatcher in isolation.)

- [ ] **Step 2: Document in `.env.example`** — verify the file exists, then add:

```bash
# SP-3 asset-class expansion. Default-OFF kill-switch: when unset/0, every
# strategy is sized down the equity (notional) path regardless of its declared
# instrument_class. Flip to 1 after soak to enable per-class sizing routing.
OPENCLAW_INSTRUMENT_CLASS_ROUTING=0
```

- [ ] **Step 3: Run**

Run: `cd /root/openclaw && python3 -m pytest tests/test_kill_switch_instrument_class.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add .env.example tests/test_kill_switch_instrument_class.py
git commit -m "feat(sp3): document OPENCLAW_INSTRUMENT_CLASS_ROUTING kill-switch (default-OFF)"
```

### Task 11: Full-suite verification + closeout (NO deploy)

- [ ] **Step 1: Run the full relevant suite**

Run: `cd /root/openclaw && python3 -m pytest tests/ -k "instrument_class or lifecycle or promotion or sizer or backtest" -v`
Expected: all green. Capture the count.

- [ ] **Step 2: Equity-parity spot check** — pick one live equity strategy, run its backtest before/after this branch (or with gate ON vs OFF) and confirm metrics are numerically identical.

- [ ] **Step 3: Update docs/memory (do NOT deploy).** Add a `## Recent Changes` entry to `/root/openclaw/CLAUDE.md`; write a `project_sp3_asset_class_expansion.md` auto-memory + index line. Note the open fast-follows (greeks-aware option sizing/backtest, leveraged-ETP decay, crypto/SP-3.1) and that the gate is default-OFF + reference strategy is `candidate`-only. **Stage by explicit name** (`git add /root/openclaw/CLAUDE.md` — the memory file is outside the repo) — never `git add -A`.

- [ ] **Step 4: Surface to operator before any merge/deploy.** Summarize: tasks done, test counts, reference-strategy backtest metrics, the basket used, and the gate state. **Do not merge to main, push, or flip the gate without operator approval** (live VPS).

---

## Self-Review (completed by author)

**Spec coverage:** §3.1 field→Tasks 1-2; resolver→Task 3; §3.2 sizing dispatcher→Task 5; §3.3 backtest→Task 6; §3.4 thresholds→Task 4; §3.5 executor→Task 8; §3.6 kill-switch→Tasks 5,10; §3.7 reference strategy→Task 9; §3.8 synthetic fixture→Task 7; §4.1 no-migration→honored (no migration task); §4.2 coverage→Task 9 Step 2; §8 verification→Task 11. All covered.

**Placeholder scan:** option threshold carries an explicit `TODO(SP-4)` (intentional, documented). Task 9 Step 4 fully specifies the signal rule. No "TBD"/"handle edge cases"/"similar to" left.

**Type consistency:** `instrument_class` (str) consistent across lifecycle/resolver/sizer/backtest; `apply_instrument_class_sizing(order, instrument_class)`, `instrument_class_for(strategy_id, manifest_path)`, `resolve_cost_model_bps(instrument_class)`, `PROMOTION_THRESHOLDS`/`_promotion_threshold` consistent between definition and call sites; gate name `OPENCLAW_INSTRUMENT_CLASS_ROUTING` consistent (Tasks 5,10).
