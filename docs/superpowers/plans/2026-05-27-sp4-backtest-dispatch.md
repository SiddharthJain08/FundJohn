# SP-4 Backtest Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `unified_backtest`'s CLI resolve each strategy's `instrument_class` (manifest-first, module-const fallback, default `equity`) and pass it to `run_backtest`, so originated/registered option strategies backtest on the Phase-0 greeks engine instead of the equity delta-1 path.

**Architecture:** Add one pure helper `_resolve_instrument_class(strategy_id, filepath=None)` to `src/backtest/unified_backtest.py` and thread its result into the three `run_backtest(...)` calls in `main()`. `run_backtest(instrument_class=...)` + `_simulate_for` already dispatch correctly (only `'option'` diverges), so this is the only missing link. Always-on; no orchestrator change, no migration, no gate.

**Tech Stack:** Python 3 (pytest). Reuses `strategies.lifecycle.VALID_INSTRUMENT_CLASSES` + `_detect_module_instrument_class` (SP-4 Phase A–D) and `backtest.options_backtest` (Phase 0).

---

## Conventions (read once)

- **Worktree:** `/root/openclaw/.claude/worktrees/sp4-backtest-dispatch` (branch `worktree-sp4-backtest-dispatch`, off `main` `ad41f79`). `data/master` is symlinked. Run everything from the worktree root.
- **Python test header** (top of the new test file):
  ```python
  from __future__ import annotations
  import sys
  from pathlib import Path
  ROOT = Path(__file__).resolve().parents[1]
  sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
  ```
- **Run a test:** `pytest tests/<file>.py -v`
- **Commit after each task**, staging only touched files; end messages with the Co-Authored-By trailer.
- **Grounding (already verified):** `unified_backtest.py` has `ROOT = Path(__file__).resolve().parents[2]` (line 55); imports `json` (39) and `from backtest import options_backtest` (62); manifest path is `ROOT / 'src' / 'strategies' / 'manifest.json'` (used at 134, 777); `_simulate_for` (579) returns `options_backtest.simulate` for `'option'` else `_per_bar_simulate`; `run_backtest(..., instrument_class='equity')` (597); `main()` calls `run_backtest` at lines ~801, ~813, ~826 without `instrument_class`.

---

## Task 0: Verify clean baseline

**Files:** none.

- [ ] **Step 1:** Run `git -C . log --oneline -1` → expect `ad41f79 Merge SP-4 Phases A-D...`; `ls -l data/master/prices.parquet` resolves (symlink).
- [ ] **Step 2:** Confirm the dispatch primitives exist:
  `python3 -c "import sys; sys.path.insert(0,'src'); from backtest.unified_backtest import _simulate_for, run_backtest; from backtest import options_backtest; print(_simulate_for('option') is options_backtest.simulate, _simulate_for('equity').__name__)"`
  Expected: `True _per_bar_simulate`.
- [ ] **Step 3:** Confirm the reference option strategy is tagged in the manifest:
  `python3 -c "import json; print(json.load(open('src/strategies/manifest.json'))['strategies']['S_short_straddle_vrp']['instrument_class'])"` → `option`.

---

## Task 1: `_resolve_instrument_class` helper + unit tests

**Files:**
- Modify: `src/backtest/unified_backtest.py` (add import near line 62; add helper after `_simulate_for`, ~line 584)
- Test: `tests/test_backtest_instrument_class_dispatch.py`

- [ ] **Step 1: Write the failing tests** — Create `tests/test_backtest_instrument_class_dispatch.py`:
```python
"""SP-4: unified_backtest CLI resolves instrument_class for dispatch.
Run: pytest tests/test_backtest_instrument_class_dispatch.py -v
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from backtest import options_backtest  # noqa: E402


def _fake_root(tmp_path, strategies: dict):
    """Build a tmp tree with src/strategies/manifest.json and point ub.ROOT at it."""
    d = tmp_path / 'src' / 'strategies'
    d.mkdir(parents=True)
    (d / 'manifest.json').write_text(json.dumps({'strategies': strategies}))
    return tmp_path


def test_manifest_option_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'option'}}))
    assert ub._resolve_instrument_class('S_x') == 'option'


def test_manifest_crypto_returned_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'crypto'}}))
    assert ub._resolve_instrument_class('S_x') == 'crypto'


def test_invalid_manifest_value_falls_back_to_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'banana'}}))
    assert ub._resolve_instrument_class('S_x') == 'equity'


def test_module_const_fallback_when_absent_from_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {}))   # S_x not in manifest
    impl = tmp_path / 'S_x.py'
    impl.write_text("INSTRUMENT_CLASS = 'option'\n\nclass X: pass\n")
    assert ub._resolve_instrument_class('S_x', filepath=str(impl)) == 'option'


def test_default_equity_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {}))
    assert ub._resolve_instrument_class('S_x') == 'equity'


def test_live_manifest_reference_option_strategy():
    # No monkeypatch: against the real worktree manifest.
    assert ub._resolve_instrument_class('S_short_straddle_vrp') == 'option'
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_backtest_instrument_class_dispatch.py -v`
  Expected: FAIL — `module 'backtest.unified_backtest' has no attribute '_resolve_instrument_class'`.

- [ ] **Step 3: Add the lifecycle import.** In `src/backtest/unified_backtest.py`, immediately after line 62 (`from backtest import options_backtest  # SP-4 Phase 0`), add:
```python
from strategies.lifecycle import VALID_INSTRUMENT_CLASSES, _detect_module_instrument_class  # noqa: E402  # SP-4 dispatch
```

- [ ] **Step 4: Add the helper** immediately after the `_simulate_for` function (after its `return _per_bar_simulate` line, ~line 584):
```python


def _resolve_instrument_class(strategy_id: str, filepath: Optional[str] = None) -> str:
    """Resolve a strategy's instrument_class for backtest dispatch.

    Precedence: (1) manifest ``strategies[strategy_id].instrument_class`` — the
    authoritative source the lifecycle promotion gate reads — accepted only if
    in VALID_INSTRUMENT_CLASSES; (2) a module-level ``INSTRUMENT_CLASS`` const in
    *filepath* (covers a freshly-coded --strategy-file not yet in the manifest),
    via lifecycle._detect_module_instrument_class; (3) 'equity'. Never raises.
    """
    try:
        manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
        entry = (json.loads(manifest_path.read_text()).get('strategies', {})
                 .get(strategy_id) or {})
        ic = entry.get('instrument_class')
        if ic in VALID_INSTRUMENT_CLASSES:
            return ic
    except Exception:
        pass
    if filepath:
        detected = _detect_module_instrument_class(filepath)
        if detected:
            return detected
    return 'equity'
```
(`_detect_module_instrument_class` already validates its result against `VALID_INSTRUMENT_CLASSES`, returning None otherwise, so step 2 needs no extra check.)

- [ ] **Step 5: Run to verify it passes** — `pytest tests/test_backtest_instrument_class_dispatch.py -v`
  Expected: PASS (6 tests).

- [ ] **Step 6: Commit**
```bash
git add src/backtest/unified_backtest.py tests/test_backtest_instrument_class_dispatch.py
git commit -m "feat(sp4): _resolve_instrument_class for unified_backtest dispatch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Wire resolution into `main()` + wiring tests

**Files:**
- Modify: `src/backtest/unified_backtest.py` (`main()`, the three `run_backtest` calls)
- Test: `tests/test_backtest_instrument_class_dispatch.py` (append)

- [ ] **Step 1: Append the failing wiring tests** to `tests/test_backtest_instrument_class_dispatch.py`:
```python


def test_main_strategy_id_threads_instrument_class(monkeypatch):
    captured = {}
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: captured.update({'sid': sid, **kw}) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--strategy-id', 'S_short_straddle_vrp'])
    assert ub.main() == 0
    assert captured['sid'] == 'S_short_straddle_vrp'
    assert captured['instrument_class'] == 'option'   # from the live manifest


def test_main_strategy_file_uses_module_const(tmp_path, monkeypatch):
    # A file whose stem is NOT in the manifest → resolver falls back to the const.
    impl = tmp_path / 'S_probe_opt.py'
    impl.write_text("INSTRUMENT_CLASS = 'option'\n\nclass X: pass\n")
    captured = {}
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: captured.update({'sid': sid, **kw}) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--strategy-file', str(impl)])
    assert ub.main() == 0
    assert captured['instrument_class'] == 'option'


def test_main_all_live_threads_per_strategy(monkeypatch):
    monkeypatch.setattr(ub, '_all_live_strategies', lambda: ['S_short_straddle_vrp'])
    seen = []
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: seen.append((sid, kw.get('instrument_class'))) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--all-live'])
    assert ub.main() == 0
    assert ('S_short_straddle_vrp', 'option') in seen


def test_resolution_composes_with_dispatch():
    # End-to-end link without running a backtest: resolved class → correct sim fn.
    assert ub._simulate_for(ub._resolve_instrument_class('S_short_straddle_vrp')) is options_backtest.simulate
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_backtest_instrument_class_dispatch.py -v -k "main or composes"`
  Expected: the `main_*` tests FAIL (`KeyError: 'instrument_class'` — main doesn't thread it yet). `test_resolution_composes_with_dispatch` may already PASS (resolution + dispatch both exist after Task 1) — that's fine.

- [ ] **Step 3: Wire the three branches.** In `src/backtest/unified_backtest.py:main()`:

  **(a) `--all-live`** — the loop currently is:
```python
        for sid in sids:
            try:
                run_backtest(sid,
                             start_date=args.start_date, end_date=args.end_date,
                             max_hold_days=args.max_hold_days)
                ok += 1
```
  change the `run_backtest` call to:
```python
        for sid in sids:
            try:
                run_backtest(sid,
                             start_date=args.start_date, end_date=args.end_date,
                             max_hold_days=args.max_hold_days,
                             instrument_class=_resolve_instrument_class(sid))
                ok += 1
```

  **(b) `--strategy-id`** — currently:
```python
    if args.strategy_id:
        try:
            run_backtest(args.strategy_id,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days)
            return 0
```
  change to:
```python
    if args.strategy_id:
        try:
            run_backtest(args.strategy_id,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days,
                         instrument_class=_resolve_instrument_class(args.strategy_id))
            return 0
```

  **(c) `--strategy-file`** — currently:
```python
    if args.strategy_file:
        sid = Path(args.strategy_file).stem
        try:
            run_backtest(sid, filepath=args.strategy_file,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days)
            return 0
```
  change to:
```python
    if args.strategy_file:
        sid = Path(args.strategy_file).stem
        try:
            run_backtest(sid, filepath=args.strategy_file,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days,
                         instrument_class=_resolve_instrument_class(sid, filepath=args.strategy_file))
            return 0
```

- [ ] **Step 4: Run to verify all pass** — `pytest tests/test_backtest_instrument_class_dispatch.py -v`
  Expected: PASS (10 tests).

- [ ] **Step 5: Commit**
```bash
git add src/backtest/unified_backtest.py tests/test_backtest_instrument_class_dispatch.py
git commit -m "feat(sp4): unified_backtest main() threads resolved instrument_class into run_backtest

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Equity byte-identity regression guard

**Files:**
- Test: `tests/test_backtest_instrument_class_dispatch.py` (append)

**Why:** lock the invariant that a normal equity strategy still resolves to `equity` (→ `_per_bar_simulate`), so the change can't silently alter the equity path.

- [ ] **Step 1: Append the test** (pick a real equity strategy id from the manifest — use the first one whose `instrument_class` is absent or `equity`):
```python
def test_equity_strategy_resolves_equity_and_dispatches_per_bar():
    import json as _json
    strategies = _json.load(open(ROOT / 'src' / 'strategies' / 'manifest.json'))['strategies']
    equity_sid = next(sid for sid, e in strategies.items()
                      if e.get('instrument_class', 'equity') == 'equity')
    assert ub._resolve_instrument_class(equity_sid) == 'equity'
    assert ub._simulate_for(ub._resolve_instrument_class(equity_sid)).__name__ == '_per_bar_simulate'
```

- [ ] **Step 2: Run** — `pytest tests/test_backtest_instrument_class_dispatch.py -v`
  Expected: PASS (11 tests).

- [ ] **Step 3: Commit**
```bash
git add tests/test_backtest_instrument_class_dispatch.py
git commit -m "test(sp4): lock equity byte-identity for backtest dispatch resolution

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final review (after all tasks)

Dispatch a whole-diff reviewer covering: (1) the resolver never raises + precedence is manifest→const→equity; (2) only `main()`'s three `run_backtest` calls changed (no change to `run_backtest`/`_simulate_for`/orchestrator/`auto_backtest`); (3) equity/etp/crypto remain `_per_bar_simulate`; (4) imports resolve (`VALID_INSTRUMENT_CLASSES`, `_detect_module_instrument_class`). Then use **superpowers:finishing-a-development-branch** — surface the merge for the operator (no migration, no gate; post-merge: regen integrity manifest on the VPS since `unified_backtest.py` is a tracked file).

---

## Self-review (spec coverage)

- **§4 helper** → Task 1 (`_resolve_instrument_class`, precedence manifest→const→equity, never raises). ✓
- **§4 wiring** → Task 2 (all three `main()` branches thread `instrument_class`). ✓
- **§3 always-on / only-option-diverges** → Task 3 (equity byte-identity guard) + Task 0 Step 2. ✓
- **§7 testing** → unit (Task 1: manifest/const/default/invalid/crypto + live-manifest option), wiring (Task 2: 3 branches via monkeypatched `run_backtest` + dispatch-link composition), regression (Task 3). ✓
- **§6 error handling** → Task 1 `try/except` + invalid-value test. ✓
- **§8 out of scope** → no orchestrator/`auto_backtest`/migration/gate changes; Final review asserts this. ✓
- **Type consistency:** `_resolve_instrument_class(strategy_id, filepath=None) -> str` signature is identical across Tasks 1–3 and the three call sites; `instrument_class=` kwarg matches `run_backtest`'s parameter (line 597).
