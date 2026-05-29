# Block-Stacked Brackets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When several strategies co-fire on a ticker, combine their exit brackets — top-sharpe rep within each correlated factor block, then stack the take-profit (capped-linear) across uncorrelated blocks while holding the stop at the tightest per-block value — instead of picking the single largest-weight bracket.

**Architecture:** A new pure module `src/execution/bracket_stacking.py` (sibling to `orthogonalization.py`) computes the stacked bracket from the per-ticker contributing brackets plus the orthogonalization block substrate already loaded by the sizer. It is wired into `regime_blended_sizer._sharpe_cadence_path` behind the default-OFF `OPENCLAW_STRATEGY_BRACKET_STACK` gate and returns the exact dict shape `_select_bracket` returns, so the downstream order builder is unchanged. A counterfactual backtest script measures the policy on historical co-firing events before any live flip.

**Tech Stack:** Python 3, psycopg2 (DB read in backtest only), pandas (backtest), pytest. No new deps, no migration, no master-data writes.

**Spec:** `docs/superpowers/specs/2026-05-29-block-stacked-brackets-design.md`

---

### Task 1: Pure `bracket_stacking` module

**Files:**
- Create: `src/execution/bracket_stacking.py`
- Test: `tests/test_bracket_stacking.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bracket_stacking.py
import math
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import bracket_stacking as bs


def _b(sid, direction, weight, entry, stop, t1, t2=None):
    return {'sid': sid, 'direction': direction, 'weight': weight,
            'entry': entry, 'stop': stop, 't1': t1, 't2': t2}


def test_single_block_returns_top_sharpe_rep_not_max_weight():
    # Two strategies, SAME block, opposite (weight, sharpe) ranking.
    # A: high weight, low sharpe, tight stop / 5% target
    # B: low weight, high sharpe, wide stop / 8% target
    cands = [
        _b('A', 1, 9.0, 100.0, 98.0, 105.0),    # stop 2%, tp 5%
        _b('B', 1, 1.0, 100.0, 94.0, 108.0),    # stop 6%, tp 8%
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'A': 7, 'B': 7},
                             eff_sharpe={'A': 0.5, 'B': 3.0})
    # One block -> rep is B (max sharpe). tp_total = 8%, stop = 6%.
    assert out['n_blocks'] == 1
    assert math.isclose(out['t1'], 108.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 94.0, rel_tol=1e-9)


def test_two_uncorrelated_blocks_stack_tp_keep_tightest_stop():
    cands = [
        _b('A', 1, 5.0, 100.0, 98.0, 105.0),    # block 1: stop 2%, tp 5%
        _b('B', 1, 5.0, 100.0, 96.0, 105.0),    # block 2: stop 4%, tp 5%
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'A': 1, 'B': 2},
                             eff_sharpe={'A': 2.0, 'B': 1.0})
    assert out['n_blocks'] == 2
    # tp stacks: 5% + 5% = 10% (under 3x cap). stop = min(2%,4%) = 2%.
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 98.0, rel_tol=1e-9)


def test_tp_cap_is_three_times_largest_single_block():
    # Four blocks each tp 5% -> sum 20%, cap = 3 * 5% = 15%.
    cands = [_b(s, 1, 1.0, 100.0, 99.0, 105.0) for s in ('A', 'B', 'C', 'D')]
    out = bs.stacked_bracket(cands, 1,
                             block_map={'A': 1, 'B': 2, 'C': 3, 'D': 4},
                             eff_sharpe={s: 1.0 for s in ('A', 'B', 'C', 'D')})
    assert out['n_blocks'] == 4
    assert math.isclose(out['t1'], 115.0, rel_tol=1e-9)   # capped at +15%
    assert math.isclose(out['stop'], 99.0, rel_tol=1e-9)  # tightest 1%


def test_short_side_mirrors_long():
    cands = [
        _b('A', -1, 5.0, 100.0, 102.0, 95.0),   # block1: stop 2%, tp 5%
        _b('B', -1, 5.0, 100.0, 103.0, 95.0),   # block2: stop 3%, tp 5%
    ]
    out = bs.stacked_bracket(cands, -1, block_map={'A': 1, 'B': 2},
                             eff_sharpe={'A': 2.0, 'B': 1.0})
    # tp stacks to 10% -> t1 = 90; stop = min(2%,3%) -> 102.
    assert math.isclose(out['t1'], 90.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 102.0, rel_tol=1e-9)


def test_ungrouped_strategies_are_singleton_blocks():
    # No block_map entries -> each is its own block -> tp stacks.
    cands = [
        _b('A', 1, 1.0, 100.0, 98.0, 105.0),
        _b('B', 1, 1.0, 100.0, 97.0, 105.0),
    ]
    out = bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={'A': 1.0, 'B': 1.0})
    assert out['n_blocks'] == 2
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 97.0, rel_tol=1e-9)


def test_wrong_direction_and_nonfinite_rejected():
    cands = [
        _b('A', -1, 9.0, 100.0, 98.0, 105.0),               # wrong direction
        _b('B', 1, 1.0, 100.0, float('nan'), 105.0),        # nonfinite stop
        _b('C', 1, 1.0, 100.0, 102.0, 105.0),               # inverted (stop>entry long)
        _b('D', 1, 1.0, 100.0, 98.0, 106.0),                # the only usable long
    ]
    out = bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={})
    assert out['n_blocks'] == 1
    assert out['why'].startswith('stacked')
    assert math.isclose(out['t1'], 106.0, rel_tol=1e-9)


def test_no_usable_bracket_returns_empty():
    cands = [_b('A', -1, 1.0, 100.0, 98.0, 105.0)]          # only wrong-direction
    assert bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={}) == {}
    assert bs.stacked_bracket([], 1, block_map={}, eff_sharpe={}) == {}


def test_rep_tiebreak_is_deterministic_smallest_sid():
    # Equal sharpe in one block -> smallest sid wins (matches representatives()).
    cands = [
        _b('Zzz', 1, 1.0, 100.0, 98.0, 105.0),
        _b('Aaa', 1, 1.0, 100.0, 95.0, 109.0),
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'Zzz': 1, 'Aaa': 1},
                             eff_sharpe={'Zzz': 2.0, 'Aaa': 2.0})
    # Tie on sharpe -> 'Aaa' chosen -> stop 5%, tp 9%.
    assert math.isclose(out['stop'], 95.0, rel_tol=1e-9)
    assert math.isclose(out['t1'], 109.0, rel_tol=1e-9)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.bracket_stacking'`

- [ ] **Step 3: Write the implementation**

```python
# src/execution/bracket_stacking.py
#!/usr/bin/env python3
"""Block-stacked brackets — Tier-3 of strategy orthogonalization.

When multiple strategies co-fire on a ticker, combine their exit brackets instead
of picking one (the legacy regime_blended_sizer._select_bracket max-weight pick):
  * within a correlated factor block -> the top-effective-sharpe member's bracket
  * across uncorrelated blocks        -> stack the take-profit (capped-linear),
                                         keep the stop at the tightest (min) per-block value.

Pure: no I/O, no DB. Returns the SAME dict shape as
regime_blended_sizer._select_bracket (entry/stop/t1/t2/weight/direction) so the
downstream order builder is unchanged. {} means "no usable bracket" -> caller
falls back to _select_bracket.

Spec: docs/superpowers/specs/2026-05-29-block-stacked-brackets-design.md
"""
from __future__ import annotations
import math
import os

TP_CAP_MULT = float(os.environ.get('OPENCLAW_BRACKET_STACK_TP_CAP_MULT', '3.0'))


def _finite(x) -> bool:
    if x is None:
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _to_fractions(b: dict, dir_sign: int):
    """(stop_pct, tp_pct) as positive fractions of entry, or None if degenerate."""
    if not (_finite(b.get('entry')) and _finite(b.get('stop')) and _finite(b.get('t1'))):
        return None
    e = float(b['entry']); s = float(b['stop']); t = float(b['t1'])
    if e <= 0:
        return None
    if dir_sign > 0:                       # long
        stop_pct = (e - s) / e
        tp_pct = (t - e) / e
    else:                                  # short
        stop_pct = (s - e) / e
        tp_pct = (e - t) / e
    if stop_pct <= 0 or tp_pct <= 0:       # inverted / degenerate
        return None
    return stop_pct, tp_pct


def _pick_top_sharpe(members: list[dict], eff_sharpe: dict[str, float]) -> dict:
    """Highest effective_sharpe member; ties broken by smallest sid (matches
    strategy_similarity.representatives determinism)."""
    return max(sorted(members, key=lambda m: str(m['sid'] or '')),
               key=lambda m: eff_sharpe.get(m['sid'], float('-inf')))


def stacked_bracket(brackets: list[dict], dir_sign: int,
                    block_map: dict[str, int], eff_sharpe: dict[str, float],
                    tp_cap_mult: float = TP_CAP_MULT) -> dict:
    """Combine direction-aligned contributing brackets into one stacked bracket.
    Returns {} when no usable bracket exists (caller falls back)."""
    # 1. Filter to winning direction + finite; convert to fractions of entry.
    usable: list[dict] = []
    for b in brackets:
        if b.get('direction') != dir_sign:
            continue
        fr = _to_fractions(b, dir_sign)
        if fr is None:
            continue
        stop_pct, tp_pct = fr
        usable.append({'sid': b.get('sid'), 'entry': float(b['entry']),
                       't2': b.get('t2'), 'weight': float(b.get('weight') or 0.0),
                       'stop_pct': stop_pct, 'tp_pct': tp_pct})
    if not usable:
        return {}

    # 2. Group by factor block; ungrouped sid -> its own singleton block.
    groups: dict[int, list[dict]] = {}
    singleton_seq = -1
    for u in usable:
        bid = block_map.get(u['sid'])
        if bid is None:
            bid = singleton_seq
            singleton_seq -= 1
        groups.setdefault(bid, []).append(u)

    # 3. Per-block representative = top-effective-sharpe member.
    reps = [_pick_top_sharpe(members, eff_sharpe) for members in groups.values()]

    # 4. Combine across blocks: stop = tightest; tp = capped-linear sum.
    stop_total = min(r['stop_pct'] for r in reps)
    tp_sum = sum(r['tp_pct'] for r in reps)
    tp_max = max(r['tp_pct'] for r in reps)
    tp_total = min(tp_sum, tp_cap_mult * tp_max)

    # 5. Anchor to the highest-sharpe block rep; rebuild absolute levels.
    anchor = _pick_top_sharpe(reps, eff_sharpe)
    entry = anchor['entry']
    if dir_sign > 0:
        stop = entry * (1.0 - stop_total)
        t1 = entry * (1.0 + tp_total)
    else:
        stop = entry * (1.0 + stop_total)
        t1 = entry * (1.0 - tp_total)
    return {
        'entry': entry, 'stop': stop, 't1': t1, 't2': anchor['t2'],
        'weight': max(r['weight'] for r in reps),
        'direction': dir_sign, 'n_blocks': len(reps),
        'why': (f'stacked tp={tp_total:.4f}(sum={tp_sum:.4f},'
                f'cap={tp_cap_mult * tp_max:.4f}) stop={stop_total:.4f} '
                f'blocks={len(reps)}'),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/bracket_stacking.py tests/test_bracket_stacking.py
git commit -m "feat(sizer): pure block-stacked bracket module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire `stacked_bracket` into the sizer (gated, via `_choose_bracket`)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (bracket dict ~line 414; substrate-load condition ~line 373; new `_choose_bracket` helper; order loop ~line 615)
- Test: `tests/test_bracket_stacking_sizer.py`

The gate decision is extracted into a small pure helper `_choose_bracket` so it is
unit-testable without a DB. The order loop calls it; `_select_bracket` is unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bracket_stacking_sizer.py
import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import regime_blended_sizer as rbs


def _candidates():
    # Two uncorrelated blocks, both long, both tp 5%, different stops.
    return [
        {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 100.0,
         'stop': 98.0, 't1': 105.0, 't2': None},
        {'sid': 'B', 'direction': 1, 'weight': 9.0, 'entry': 100.0,
         'stop': 96.0, 't1': 105.0, 't2': None},
    ]


_GROUPS = {'block_map': {'A': 1, 'B': 2}}
_SHARPE = {'A': 2.0, 'B': 1.0}


def test_choose_bracket_gate_off_picks_max_weight(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    out = rbs._choose_bracket(_candidates(), 1, _GROUPS, _SHARPE)
    # Legacy: max-weight pick (B), single bracket, no stacking.
    assert out['sid'] == 'B'
    assert math.isclose(out['t1'], 105.0, rel_tol=1e-9)


def test_choose_bracket_gate_on_stacks(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    out = rbs._choose_bracket(_candidates(), 1, _GROUPS, _SHARPE)
    assert out['n_blocks'] == 2
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)   # 5% + 5% stacked
    assert math.isclose(out['stop'], 98.0, rel_tol=1e-9)  # tightest (block A, 2%)


def test_choose_bracket_gate_on_no_substrate_falls_back(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    out = rbs._choose_bracket(_candidates(), 1, None, _SHARPE)
    assert out['sid'] == 'B'                              # ortho_groups None -> legacy


def test_choose_bracket_gate_on_empty_stack_falls_back(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    # All wrong-direction -> stacked_bracket {} AND _select_bracket {} -> {}.
    cands = [{'sid': 'A', 'direction': -1, 'weight': 1.0, 'entry': 100.0,
              'stop': 98.0, 't1': 105.0, 't2': None}]
    assert rbs._choose_bracket(cands, 1, {'block_map': {}}, {}) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking_sizer.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_choose_bracket'`

- [ ] **Step 3: Carry `sid` in the per-signal bracket dict**

In `src/execution/regime_blended_sizer.py`, the bracket dict appended inside the
`for s in active:` loop (~line 414) gains `sid` as the first key (so each bracket
maps to its block; `_select_bracket` ignores the extra key → OFF path unchanged):

```python
        ticker_meta[tkr]['brackets'].append({
            'sid':        sid,
            'direction':  d,
            'weight':     weight_by_strat[sid],
            'entry':      s.get('entry_price'),
            'stop':       s.get('stop_loss'),
            't1':         s.get('target_1'),
            't2':         s.get('target_2'),
        })
```

`_select_bracket` returns `usable[0]` verbatim (confirmed at ~line 682), so it now
returns a dict carrying `sid` with no change to that function.

- [ ] **Step 4: Add `_choose_bracket` + extend the substrate-load condition + call it in the order loop**

Add the helper next to `_select_bracket` (after it, ~line 683):

```python
def _choose_bracket(candidates: list[dict], dir_sign: int,
                    ortho_groups: dict | None, sharpe_by_strat: dict) -> dict:
    """Gate decision for the bracket attached to an emission.
    OPENCLAW_STRATEGY_BRACKET_STACK ON + block substrate present -> stacked bracket
    (falls back to the legacy max-weight _select_bracket if stacking yields nothing).
    OFF (or no substrate) -> _select_bracket, byte-identical to legacy."""
    if ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        from execution import bracket_stacking as _bs
        stacked = _bs.stacked_bracket(candidates, dir_sign,
                                      ortho_groups['block_map'], sharpe_by_strat)
        if stacked:
            return stacked
    return _select_bracket(candidates, dir_sign)
```

Extend the substrate-load condition (~line 373) so the block substrate loads when
bracket-stacking is on:

```python
    if _ortho_enabled('OPENCLAW_STRATEGY_FOLD') or _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT') \
            or _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            or _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
```

Replace the bracket selection in the order loop (~line 615-618):

```python
        if kind in ('orphan_close', 'flip_close'):
            bracket = {}     # forces close_only=True downstream
        else:
            bracket = _choose_bracket(ticker_meta[tkr].get('brackets', []),
                                      dir_sign, _ortho_groups, sharpe_by_strat)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking_sizer.py -q`
Expected: PASS (4 passed)

- [ ] **Step 6: Regression — confirm gate-OFF leaves existing sizer tests green**

Run: `cd /root/openclaw && python3 -m pytest tests/test_orthogonalization_sizer.py tests/test_sizer_no_oue_multiplier.py -q`
Expected: PASS (all pre-existing assertions still hold; the only diff is the additive `sid` key in the bracket dict, which existing tests don't assert against).

- [ ] **Step 7: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/test_bracket_stacking_sizer.py
git commit -m "feat(sizer): gate block-stacked brackets into sharpe-cadence path

OPENCLAW_STRATEGY_BRACKET_STACK (default-OFF) via _choose_bracket helper.
When OFF (or no substrate), _select_bracket path is byte-identical. Carries
sid through the bracket dict so each contributor maps to its factor block.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Shadow logging (preview without routing)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (order loop, the `else` branch ~line 628)

- [ ] **Step 1: Add the shadow log inside `_choose_bracket`**

When `OPENCLAW_STRATEGY_ORTHO_SHADOW` is on AND `OPENCLAW_STRATEGY_BRACKET_STACK`
is off, log what stacking WOULD produce vs. the bracket actually selected — no
routing change. Extend `_choose_bracket` (the helper added in Task 2) so the
legacy path also previews the stack:

```python
def _choose_bracket(candidates: list[dict], dir_sign: int,
                    ortho_groups: dict | None, sharpe_by_strat: dict) -> dict:
    """Gate decision for the bracket attached to an emission.
    OPENCLAW_STRATEGY_BRACKET_STACK ON + block substrate present -> stacked bracket
    (falls back to the legacy max-weight _select_bracket if stacking yields nothing).
    OFF (or no substrate) -> _select_bracket, byte-identical to legacy.
    Under ORTHO_SHADOW (and stacking OFF) it logs the would-be stacked bracket."""
    if ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        from execution import bracket_stacking as _bs
        stacked = _bs.stacked_bracket(candidates, dir_sign,
                                      ortho_groups['block_map'], sharpe_by_strat)
        if stacked:
            return stacked
    selected = _select_bracket(candidates, dir_sign)
    if ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            and not _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        try:
            from execution import bracket_stacking as _bs
            shadow_b = _bs.stacked_bracket(candidates, dir_sign,
                                           ortho_groups['block_map'], sharpe_by_strat)
            if shadow_b:
                logger.info(
                    'bracket_stack.shadow: selected(stop=%.2f t1=%.2f) vs '
                    'stacked(stop=%.2f t1=%.2f n=%d) %s',
                    float(selected.get('stop') or 0.0), float(selected.get('t1') or 0.0),
                    shadow_b['stop'], shadow_b['t1'], shadow_b['n_blocks'], shadow_b['why'])
        except Exception as _e:
            logger.warning('bracket_stack.shadow failed (%s)', _e)
    return selected
```

- [ ] **Step 2: Smoke — import + byte-identical gate-OFF still holds**

Run: `cd /root/openclaw && python3 -c "import sys; sys.path.insert(0,'src'); from execution import regime_blended_sizer; print('import ok')"`
Expected: `import ok`

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking_sizer.py -q`
Expected: PASS (4 passed) — shadow path is inert under the test envs (none set `OPENCLAW_STRATEGY_ORTHO_SHADOW`).

- [ ] **Step 3: Commit**

```bash
git add src/execution/regime_blended_sizer.py
git commit -m "feat(sizer): shadow-log would-be stacked bracket vs selected (no routing)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Counterfactual backtest script

**Files:**
- Create: `scripts/backtest_bracket_stacking.py`
- Test: `tests/test_bracket_stacking_backtest.py`

- [ ] **Step 1: Write the failing test for the pure comparison core**

```python
# tests/test_bracket_stacking_backtest.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pandas as pd
from scripts.backtest_bracket_stacking import compare_event


def _bars():
    # 6 forward sessions; rises ~+12% then pulls back. high tags 110 on day 3,
    # 112 on day 4, never hits 115.
    idx = pd.to_datetime(['2026-01-02', '2026-01-03', '2026-01-04',
                          '2026-01-05', '2026-01-06', '2026-01-07'])
    return pd.DataFrame({
        'high':  [103, 108, 110, 112, 111, 109],
        'low':   [ 99, 102, 106, 108, 107, 105],
        'close': [102, 107, 109, 111, 109, 106],
    }, index=idx)


def test_compare_event_distinguishes_policies():
    # Two uncorrelated blocks, both long, tp 5% (105), stops 2% & 4%.
    cands = [
        {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 100.0,
         'stop': 98.0, 't1': 105.0, 't2': None},
        {'sid': 'B', 'direction': 1, 'weight': 9.0, 'entry': 100.0,
         'stop': 96.0, 't1': 105.0, 't2': None},
    ]
    res = compare_event(
        ticker='TST', dir_sign=1, candidates=cands,
        bars=_bars(), entry_date=pd.Timestamp('2026-01-01'),
        block_map={'A': 1, 'B': 2}, eff_sharpe={'A': 2.0, 'B': 1.0},
        max_hold_days=5)
    # current = max-weight pick (B): tp 105 -> target hit day1 (high 103? no; day2 high108>=105) -> +5%.
    assert res['current']['exit_reason'] == 'target'
    assert abs(res['current']['pnl_pct'] - 0.05) < 1e-9
    # stacked: tp 110 -> target hit day3 (high 110) -> +10%; stop tightest 98 (never hit).
    assert res['stacked']['exit_reason'] == 'target'
    assert abs(res['stacked']['pnl_pct'] - 0.10) < 1e-9
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking_backtest.py -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name 'compare_event'`

- [ ] **Step 3: Write the script**

```python
# scripts/backtest_bracket_stacking.py
#!/usr/bin/env python3
"""Counterfactual backtest: stacked bracket vs. legacy max-weight pick.

Replays historical co-firing events from execution_signals; for each event it
builds BOTH brackets (current _select_bracket pick and stacked_bracket), runs
unified_backtest.simulate_trade on the ticker's forward bars for each, and
aggregates per-trade P&L / hit-rate / exit-reason mix. This isolates the
bracket-policy effect (same entries, same tickers, different exits).

LIMITATIONS (documented, by design):
  * Size held fixed (per-unit) -> measures per-trade exit quality, not portfolio
    interaction.
  * Uses the CURRENT orthogonalization substrate (load_groups) + current
    effective_sharpe for all historical events (point-in-time substrate is not
    reconstructed). Good enough to settle the stop/TP policy.
  * Per-candidate weight is set to 1.0 (historical daily_weight is not stored on
    execution_signals), so the "current" max-weight pick degenerates to the first
    deterministic (strategy_id-ordered) finite bracket. The stacked policy does not
    depend on weight, so the comparison still isolates the exit-shape effect.
Read-only. Touches no master data.

Usage: python3 scripts/backtest_bracket_stacking.py [--days N] [--regime LOW_VOL] [--max-hold 10]
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd  # noqa: E402
from execution.regime_blended_sizer import _select_bracket, _dir_to_int  # noqa: E402
from execution import bracket_stacking as bs  # noqa: E402
from backtest.unified_backtest import simulate_trade  # noqa: E402


def compare_event(ticker, dir_sign, candidates, bars, entry_date,
                  block_map, eff_sharpe, max_hold_days):
    """Run simulate_trade for both bracket policies on one event.
    Returns {'current': exit_dict|None, 'stacked': exit_dict|None}."""
    cur = _select_bracket(candidates, dir_sign)
    stk = bs.stacked_bracket(candidates, dir_sign, block_map, eff_sharpe)
    out = {'current': None, 'stacked': None}
    for key, br in (('current', cur), ('stacked', stk)):
        if not br or br.get('entry') is None or br.get('stop') is None or br.get('t1') is None:
            continue
        out[key] = simulate_trade(
            bars=bars, entry_date=entry_date, direction=dir_sign,
            entry_price=float(br['entry']), stop_loss=float(br['stop']),
            target_1=float(br['t1']), max_hold_days=max_hold_days)
    return out


def _load_substrate(regime):
    from execution import strategy_similarity as ss
    from execution import strategy_weights as sw
    groups = ss.load_groups(regime)
    rows = sw.load_current(regime)
    eff_sharpe = {r['strategy_id']: float(r['effective_sharpe']) for r in rows}
    return groups.get('block_map', {}), eff_sharpe


def _load_events(days):
    """Co-firing events: {(signal_date, ticker): [bracket_dict, ...]} with >=1 finite bracket."""
    import psycopg2, psycopg2.extras
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('''
            SELECT DISTINCT ON (signal_date, strategy_id, ticker)
                   signal_date, strategy_id, ticker, direction,
                   entry_price, stop_loss, target_1, target_2
            FROM execution_signals
            WHERE signal_date >= CURRENT_DATE - make_interval(days => %s)
            ORDER BY signal_date, strategy_id, ticker
        ''', (days,))
        rows = cur.fetchall()
    finally:
        conn.close()
    events = defaultdict(list)
    for r in rows:
        events[(r['signal_date'], r['ticker'])].append({
            'sid': r['strategy_id'], 'direction': _dir_to_int(r['direction']),
            'weight': 1.0, 'entry': r['entry_price'], 'stop': r['stop_loss'],
            't1': r['target_1'], 't2': r['target_2'],
        })
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--regime', default='LOW_VOL')
    ap.add_argument('--max-hold', type=int, default=10)
    args = ap.parse_args()

    from backtest.unified_backtest import load_prices_panels
    _, panels = load_prices_panels()
    block_map, eff_sharpe = _load_substrate(args.regime)
    events = _load_events(args.days)

    agg = {'current': defaultdict(float), 'stacked': defaultdict(float)}
    reasons = {'current': defaultdict(int), 'stacked': defaultdict(int)}
    n_multi = 0
    n_used = 0
    for (sig_date, ticker), cands in events.items():
        # winning direction = signed-sum of dir (majority by count here; weight=1)
        net = sum(c['direction'] for c in cands)
        if net == 0:
            continue
        dir_sign = 1 if net > 0 else -1
        bars = panels.get(ticker)
        if bars is None or bars.empty:
            continue
        entry_date = pd.Timestamp(sig_date)
        n_blocks = len({block_map.get(c['sid'], c['sid']) for c in cands
                        if c['direction'] == dir_sign})
        if n_blocks >= 2:
            n_multi += 1
        res = compare_event(ticker, dir_sign, cands, bars, entry_date,
                            block_map, eff_sharpe, args.max_hold)
        if not (res['current'] and res['stacked']):
            continue
        n_used += 1
        for key in ('current', 'stacked'):
            agg[key]['pnl'] += res[key]['pnl_pct']
            agg[key]['hold'] += res[key]['holding_days']
            reasons[key][res[key]['exit_reason']] += 1

    print(f'\n=== bracket-stacking counterfactual ({args.days}d, regime={args.regime}, '
          f'events_used={n_used}, multi-block={n_multi}) ===')
    if n_used == 0:
        print('no comparable events (no forward bars / single-strategy only).')
        return
    for key in ('current', 'stacked'):
        mean = agg[key]['pnl'] / n_used
        hold = agg[key]['hold'] / n_used
        print(f'{key:8s}  mean_pnl/trade={mean:+.4f}  avg_hold={hold:5.2f}d  '
              f'exits={dict(reasons[key])}')
    delta = (agg['stacked']['pnl'] - agg['current']['pnl']) / n_used
    print(f'\nstacked - current: {delta:+.4f} mean pnl/trade over {n_used} events')


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking_backtest.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_bracket_stacking.py tests/test_bracket_stacking_backtest.py
git commit -m "feat(backtest): counterfactual stacked-bracket vs max-weight pick

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full verification + run the counterfactual on real data

**Files:** none (verification only)

- [ ] **Step 1: Full feature test sweep**

Run: `cd /root/openclaw && python3 -m pytest tests/test_bracket_stacking.py tests/test_bracket_stacking_sizer.py tests/test_bracket_stacking_backtest.py tests/test_orthogonalization.py tests/test_orthogonalization_sizer.py tests/test_sizer_no_oue_multiplier.py -q`
Expected: PASS (all)

- [ ] **Step 2: Run the counterfactual on a real window (produces the decision numbers)**

Run: `cd /root/openclaw && python3 scripts/backtest_bracket_stacking.py --days 120 --regime LOW_VOL --max-hold 10`
Expected: a comparison table printing `current` vs `stacked` mean pnl/trade, avg hold, exit-reason mix, and `events_used` / `multi-block` counts. Capture the output — this is the empirical input to the live-flip decision.

- [ ] **Step 3: Confirm gate-OFF default is byte-identical (no env set)**

Run: `cd /root/openclaw && env -u OPENCLAW_STRATEGY_BRACKET_STACK python3 -c "
import sys; sys.path.insert(0,'src')
from execution import regime_blended_sizer as rbs
cands=[{'sid':'A','direction':1,'weight':5.0,'entry':100.0,'stop':98.0,'t1':105.0,'t2':None},
       {'sid':'B','direction':1,'weight':9.0,'entry':100.0,'stop':96.0,'t1':105.0,'t2':None}]
b=rbs._select_bracket(cands,1)
assert abs(b['t1']-105.0)<1e-9 and b['sid']=='B', b
print('gate-OFF byte-identical: max-weight pick, t1=105, sid=B')
"`
Expected: `gate-OFF byte-identical: max-weight pick, t1=105, sid=B`

- [ ] **Step 4: No commit needed** (verification only). Report the counterfactual numbers in the completion summary.

---

## Notes for the executor

- **NEVER `git add -A` / `git add .`** — the working tree carries unrelated in-flight churn (`src/strategies/manifest.json`, `.superpowers/`, `manifest.json.bak-*`). Stage only the exact files named in each task.
- DB env var is `POSTGRES_URI` (not `DATABASE_URL`). `.env` contains live secrets — do not echo it.
- The gate `OPENCLAW_STRATEGY_BRACKET_STACK` ships **OFF**; this plan does NOT flip it. The live-flip decision follows the Task 5 counterfactual numbers.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
