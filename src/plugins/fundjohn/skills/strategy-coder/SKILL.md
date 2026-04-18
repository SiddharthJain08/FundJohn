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
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['{ClassName}']

class {ClassName}(BaseStrategy):
    """One-line description from strategy_spec.signal_logic."""

    STRATEGY_ID = '{strategy_id}'

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        if df.empty:
            return []
        # ... signal generation logic ...
        return signals
```

### Required class structure
- Extends `BaseStrategy` from `src/strategies/base.py`
- Implements `generate_signals(df: pd.DataFrame) -> list[Signal]`
- Returns `[]` for empty DataFrame — never raise, never return None
- Imports `REGIME_POSITION_SCALE` and `REGIME_ATR_SCALE` from base if using regime logic
- No naked imports inside method bodies
- Maximum 200 lines total

### Signal dataclass fields (from `src/strategies/base.py`)
```python
@dataclass
class Signal:
    ticker: str
    strategy_id: str
    signal_type: str       # 'LONG' | 'SHORT'
    entry_price: float
    stop_loss: float
    profit_target: float
    confidence: float      # 0.0–1.0
    regime: str            # 'LOW_VOL' | 'TRANSITIONING' | 'HIGH_VOL' | 'CRISIS'
    signal_date: str       # YYYY-MM-DD
    target_2: float = None
    target_3: float = None
    ev: float = None
    kelly: float = None
```

### Stop and target computation
Use `self.compute_stops_and_targets(prices_series, direction, current_price, regime_state=regime)` from `BaseStrategy`. Do not hand-roll ATR math.

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
