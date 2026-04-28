---
name: fundjohn:strategy-coder
description: Implement a strategy spec as a strategies/implementations/<id>.py module.
triggers:
  - strategy-spec ready for implementation
  - /strategy-coder
inputs:
  - strategy_spec
outputs:
  - strategy_module_py
  - manifest_entry
keywords: [strategy, coder, implementation, manifest]
---
# Skill: fundjohn:strategy-coder
**Trigger**: `/strategy-coder` or `/code-strategy`

## Purpose
Turn a `strategy_spec` JSON (from `fundjohn:paper-to-strategy`) into three commit-ready artifacts:
1. `src/strategies/implementations/{strategy_id}.py`
2. `_IMPL_MAP` entry for `src/strategies/registry.py`
3. Manifest JSON block for `src/strategies/manifest.json`

You stop explaining the contract. Load this skill and produce the artifacts.

## Artifact 1 — Strategy Implementation File

```python
# src/strategies/implementations/{strategy_id}.py
from __future__ import annotations
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['{ClassName}']

class {ClassName}(BaseStrategy):
    """One-line description from strategy_spec.signal_logic."""

    id          = '{strategy_id}'
    name        = '{ClassName}'
    description = '{signal_logic one-liner from strategy_spec}'
    tier        = 2

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)
        # ... signal generation logic ...
        return signals
```

### Required class structure
- Extends `BaseStrategy` from `src/strategies/base.py`
- Implements `generate_signals(prices, regime, universe, aux_data=None) -> List[Signal]` — 4-argument signature required
- Returns `[]` for empty/None prices — never raise, never return None
- Call `self.should_run(regime_state)` early; return `[]` if False
- Call `self.position_scale(regime_state)` for regime-adjusted sizing
- No naked imports inside method bodies
- Maximum 200 lines total

### Signal dataclass fields — exact definition from `src/strategies/base.py`
```python
Signal(
    ticker            = str,      # e.g. 'AAPL'
    direction         = str,      # 'LONG' | 'SHORT' | 'SELL_VOL' | 'BUY_VOL' | 'FLAT'
    entry_price       = float,
    stop_loss         = float,
    target_1          = float,
    target_2          = float,
    target_3          = float,
    position_size_pct = float,    # fraction of portfolio (0.0–1.0), apply regime scale
    confidence        = str,      # 'HIGH' | 'MED' | 'LOW' — NEVER a float, NEVER None
    signal_params     = dict,     # optional extra metadata
)
```

### Stop and target computation
Use `self.compute_stops_and_targets(prices_series, direction, current_price, regime_state=regime_state)` from `BaseStrategy`. Do not hand-roll ATR math.

## Artifact 2 — Registry Entry

One line to add to `_IMPL_MAP` in `src/strategies/registry.py`:
```python
'{strategy_id}': ('strategies.implementations.{strategy_id}', '{ClassName}'),
```

## Artifact 3 — Manifest JSON Block

```json
{
  "state": "candidate",
  "state_since": "{today_iso}",
  "metadata": {
    "canonical_file": "{strategy_id}.py",
    "class": "{ClassName}",
    "description": "{signal_logic one-liner from strategy_spec}"
  },
  "history": []
}
```

Add this under the strategy_id key in the `"strategies"` section of `manifest.json`.

## Hard Rules
- Never edit `src/strategies/base.py` or `src/strategies/lifecycle.py`
- Always produce all 3 artifacts — never partial output
- State = `candidate` always; lifecycle promotion is BotJohn's decision
- Run a mental dry-run: call `generate_signals(pd.DataFrame())` in your head — must return `[]`
