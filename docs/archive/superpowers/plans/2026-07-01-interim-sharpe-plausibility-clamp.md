# Interim Backtest-Sharpe Plausibility Clamp — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clamp each per-regime backtest Sharpe to `[−CAP, +CAP]` (default 3.0, `pipeline_config`-tunable) at the sizer's weekly rebuild, so no strategy's weight or corr-gate contribution is driven by the inflated-magnitude backtest-Sharpe artifact — while preserving every current inclusion/exclusion decision.

**Architecture:** Two small additions to `src/execution/strategy_weights.py`: a config reader `_get_bt_sharpe_cap(cur)` (mirrors `oue_classifier._get_sigma_gate`) and a pure clamp helper `_clamp_bt_sharpes(out, cap)`, invoked once at the end of `_load_backtest_sharpe` before it returns. Clamped `bt_sharpe` flows through the existing `_effective_sharpe → weight → strategy_weights_by_regime` path, so both the sizer (`load_current`) and the corr-adjusted conviction gate consume bounded values. No schema migration; no restart (Python re-imports per rebuild).

**Tech Stack:** Python 3, psycopg2, `unittest`. Spec: `docs/superpowers/specs/2026-07-01-interim-sharpe-plausibility-clamp-design.md`.

## Global Constraints

- PATH-SCOPED commits only — stage EXACTLY `src/execution/strategy_weights.py` and `tests/test_bt_sharpe_clamp.py`. NEVER `git add -A`/`.`. The live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `src/strategies/registry.py`, untracked `src/strategies/implementations/S_*`, `scripts/first_wide_fill_watcher.py`) — do not stage or touch it. Verify the staged set before committing.
- Do NOT push, restart johnbot, run a live rebuild, or apply anything to the live DB. Tests are pure/offline (mock cursor; no DB).
- Sign-preservation is an INVARIANT: a clamp must never move a value across zero (a negative stays negative → stays excluded by `_is_sizeable_sharpe`; a positive stays positive → stays included). `low_volatility_us` (−8.57) must NOT become fundable.
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from `/root/openclaw`.
- Default CAP is exactly `3.0`; `pipeline_config` key is exactly `bt_sharpe_plausibility_cap`.

---

### Task 1: Config reader + pure clamp helper + wiring in `_load_backtest_sharpe`

**Files:**
- Modify: `src/execution/strategy_weights.py` (add two helpers near the existing `_get_sigma_gate`-style helpers / above `_load_backtest_sharpe` at line 294; wire the clamp in before `_load_backtest_sharpe`'s `return out` at line 383)
- Test: `tests/test_bt_sharpe_clamp.py` (create)

**Interfaces:**
- Consumes: `strategy_weights` already has `import math` (line 51) and `logger = logging.getLogger(__name__)` (line 55). `_load_backtest_sharpe(conn, strategy_ids)` builds `out: dict[tuple[str,str], dict]` where each value is `{'bt_sharpe': float|None, 'bt_n': int|None}`, and holds a `cur = conn.cursor(...)` (DictCursor). `pipeline_config` is a KV table read via `SELECT value FROM pipeline_config WHERE key=%s`.
- Produces: `_get_bt_sharpe_cap(cur) -> float`; `_clamp_bt_sharpes(out: dict, cap: float) -> list[tuple[tuple[str,str], float, float]]` (mutates `out` in place, returns the list of `(key, before, after)` for entries actually clamped).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bt_sharpe_clamp.py`:
```python
"""tests/test_bt_sharpe_clamp.py — interim backtest-Sharpe plausibility clamp
(§7 metric recon). Verifies the pipeline_config reader defaults safely and the
clamp is sign-preserving and disable-able."""
from __future__ import annotations
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import strategy_weights as sw  # noqa: E402


class FakeCur:
    """Minimal cursor stub: fetchone returns the queued row; optionally raises."""
    def __init__(self, row=None, raise_on_execute=False):
        self._row = row
        self._raise = raise_on_execute
    def execute(self, *a, **k):
        if self._raise:
            raise RuntimeError('boom')
    def fetchone(self):
        return self._row


class TestGetCap(unittest.TestCase):
    def test_present_value_parsed(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=('2.5',))), 2.5)
    def test_absent_key_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=None)), 3.0)
    def test_malformed_value_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=('abc',))), 3.0)
    def test_query_error_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(raise_on_execute=True)), 3.0)


class TestClamp(unittest.TestCase):
    def test_inflated_positive_capped(self):
        out = {('A', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 100}}
        clamped = sw._clamp_bt_sharpes(out, 3.0)
        self.assertEqual(out[('A', 'LOW_VOL')]['bt_sharpe'], 3.0)
        self.assertEqual(clamped, [(('A', 'LOW_VOL'), 14.0, 3.0)])

    def test_inflated_negative_capped_still_negative(self):
        out = {('B', 'LOW_VOL'): {'bt_sharpe': -8.57, 'bt_n': 100}}
        sw._clamp_bt_sharpes(out, 3.0)
        self.assertEqual(out[('B', 'LOW_VOL')]['bt_sharpe'], -3.0)  # still < 0 -> excluded

    def test_within_band_untouched(self):
        out = {('C', 'LOW_VOL'): {'bt_sharpe': 1.2, 'bt_n': 50}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertEqual(out[('C', 'LOW_VOL')]['bt_sharpe'], 1.2)

    def test_none_untouched(self):
        out = {('D', 'LOW_VOL'): {'bt_sharpe': None, 'bt_n': None}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertIsNone(out[('D', 'LOW_VOL')]['bt_sharpe'])

    def test_nonfinite_untouched(self):
        out = {('E', 'HIGH_VOL'): {'bt_sharpe': float('nan'), 'bt_n': 5},
               ('F', 'HIGH_VOL'): {'bt_sharpe': float('inf'), 'bt_n': 5}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertTrue(math.isnan(out[('E', 'HIGH_VOL')]['bt_sharpe']))
        self.assertTrue(math.isinf(out[('F', 'HIGH_VOL')]['bt_sharpe']))

    def test_disable_via_high_cap(self):
        out = {('G', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 100}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 999.0), [])
        self.assertEqual(out[('G', 'LOW_VOL')]['bt_sharpe'], 14.0)

    def test_sign_never_crosses_zero(self):
        out = {('H', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 1},
               ('I', 'LOW_VOL'): {'bt_sharpe': -8.5, 'bt_n': 1}}
        for key, before, after in sw._clamp_bt_sharpes(out, 3.0):
            self.assertTrue((before > 0) == (after > 0) and (before < 0) == (after < 0))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_bt_sharpe_clamp.py`
Expected: FAIL — `AttributeError: module 'execution.strategy_weights' has no attribute '_get_bt_sharpe_cap'`.

- [ ] **Step 3: Implement the two helpers**

In `src/execution/strategy_weights.py`, add ABOVE `def _load_backtest_sharpe` (line 294):
```python
def _get_bt_sharpe_cap(cur) -> float:
    """Read bt_sharpe_plausibility_cap from pipeline_config; default 3.0.
    Interim guard (§7 metric recon, 2026-07-01) for the backtest-Sharpe
    methodology defect (flat P&L smearing understates vol -> inflated,
    overlap-dependent |Sharpe|). Set the key very high (e.g. 999) to disable.
    Fail-safe: any read error keeps the guard ON at the 3.0 default."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='bt_sharpe_plausibility_cap'")
        row = cur.fetchone()
        if row:
            return float(row[0])
    except Exception:
        pass
    return 3.0


def _clamp_bt_sharpes(out: dict, cap: float) -> list:
    """Clamp each entry's bt_sharpe to [-cap, +cap] IN PLACE (sign-preserving,
    so no strategy changes inclusion/exclusion state). Leaves None / NaN / Inf
    untouched (the _is_sizeable_sharpe guard drops those). Returns the list of
    (key, before, after) tuples for entries actually clamped."""
    clamped = []
    for key, v in out.items():
        s = v.get('bt_sharpe')
        if s is not None and math.isfinite(s):
            c = max(-cap, min(cap, s))
            if c != s:
                clamped.append((key, s, c))
                v['bt_sharpe'] = c
    return clamped
```

- [ ] **Step 4: Wire the clamp into `_load_backtest_sharpe`**

In `_load_backtest_sharpe`, replace the final `return out` (line 383) with:
```python
    cap = _get_bt_sharpe_cap(cur)
    clamped = _clamp_bt_sharpes(out, cap)
    if clamped:
        logger.info('bt_sharpe plausibility clamp: %d/%d entries clamped to ±%s (e.g. %s %.2f->%.2f)',
                    len(clamped), len(out), cap, clamped[0][0], clamped[0][1], clamped[0][2])
    return out
```
(`cur` is the DictCursor already opened at the top of `_load_backtest_sharpe`. If the Tier-1 `except` block ran `conn.rollback()`, `cur` is still usable for this SELECT.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_bt_sharpe_clamp.py`
Expected: `OK` (11 tests). Also `python3 -c "import ast; ast.parse(open('src/execution/strategy_weights.py').read())"` → no output (syntax OK).

- [ ] **Step 6: Regression — confirm the sizer module still imports and its existing tests pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -c "from execution import strategy_weights"` (expect no error).
If `tests/test_strategy_weights*.py` exists, run it; otherwise skip (none today). Report what you ran.

- [ ] **Step 7: Commit (path-scoped)**

```bash
cd /root/openclaw
git add src/execution/strategy_weights.py tests/test_bt_sharpe_clamp.py
git status --porcelain   # MUST show ONLY those two paths staged
git commit -m "feat(sizer): interim backtest-Sharpe plausibility clamp (§7 metric recon)

Clamp per-regime bt_sharpe to [-CAP,+CAP] (pipeline_config
bt_sharpe_plausibility_cap, default 3.0) at rebuild, bounding weight +
corr-gate contribution from the inflated backtest-Sharpe artifact.
Sign-preserving: no inclusion/exclusion change. 11 tests.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** Component 1 (`_get_bt_sharpe_cap`) → Steps 1/3. Component 2 (`_clamp_bt_sharpes` + wiring) → Steps 3/4. Symmetric clamp, sign-preservation, None/NaN/Inf handling, disable path, default 3.0, key name → all covered by tests in Step 1. Rollout/verify steps live in the spec (operator-gated deploy, not in this build). No schema migration (matches spec). ✓
**Placeholder scan:** none — all code is concrete and runnable. ✓
**Type consistency:** `_clamp_bt_sharpes(out, cap)` returns `list[(key, before, after)]`; the wiring in Step 4 indexes `clamped[0][0/1/2]` consistently; `_get_bt_sharpe_cap(cur) -> float`. ✓
