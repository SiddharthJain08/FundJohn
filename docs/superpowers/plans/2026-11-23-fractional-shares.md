# Fractional Shares Across the Board — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every equity LONG order (open, reduce, close) is submitted at its exact fractional quantity; shorts are floored to whole shares and never forced above target.

**Architecture:** One pure sizing helper in `alpaca_executor.py` (`_size_qty`) replaces every `max(1, int(...))`; the reduce path converts the dollar delta into a decimal `--qty` from the live position qty instead of `--percentage`; `stop_reattach` carries decimal quantities and uses DAY time-in-force for fractional positions (GTC stays for whole-share positions). No sizer change — the sizer already emits dollar targets with a $1 dust floor.

**Tech Stack:** Python 3, pytest, Alpaca CLI (`/root/go/bin/alpaca`) + REST (OCO), PostgreSQL (`alpaca_submissions.qty` numeric).

**Spec:** `docs/superpowers/specs/2026-11-23-fractional-shares-design.md`

## Global Constraints
- Fractional orders are LONG-ONLY at Alpaca; a short order quantity is always an integer ≥ 1.
- Fractional orders use `time_in_force='day'`; a GTC order with a fractional qty is rejected.
- Fractional qty formatted with up to 6 decimals, no trailing zeros, never scientific notation (`_qty_str`).
- Dust floor: an order whose notional < `DUST_FLOOR_USD` (1.0, imported from `execution.regime_blended_sizer`) is SKIPPED with reason `dust_below_1usd`.
- Options (`--order-class mleg`, OCC symbols) and crypto (`-USD`) paths are untouched.
- Never remove an existing log line's prefix; `x{qty} sh` renders via `_qty_str`.
- Tests: run only the named files (`python3 -m pytest -q <files>`), never the full suite; tests must not reach the broker (stub `_run_alpaca_cli` / `_alpaca_rest_post`).

## Review Focus
1. A long target of $0.80 on a $40 stock (0.02 sh, < $1): must SKIP `dust_below_1usd`, not submit 0.02 sh. → Task 1 test.
2. A short target of 0.9 sh: must SKIP `short_sub_share`, never round up to 1. → Task 1 test.
3. A long on a NON-fractionable asset with target 0.6 sh: SKIP `not_fractionable_sub_share`; target 2.6 sh → 2 sh (floor, logged). → Task 1 test.
4. A partial reduce of a 1-share SHORT by 70 %: reduce qty floors to 0 → SKIP `reduce_rounds_to_zero` (no broker call); a full exit still closes. → Task 2 test.
5. A fractional LONG position in stop_reattach: stop/OCO qty is decimal and TIF is `day`; a whole-share position keeps `gtc`. → Task 3 test.

---

### Task 1: `_size_qty` + open / close-only paths

**Files:**
- Modify: `src/execution/alpaca_executor.py` (open path ~L2641 `qty = max(1, int(notional / entry))`; close-only path ~L2596 `qty_co = max(1, int(notional_co / lp_co))`; OPG/day-sweep casts ~L2455, L2491, L2546 `int(...)` → `float(...)`; log lines `x{qty} sh` → `x{_qty_str(qty)} sh`)
- Test: `tests/execution/test_alpaca_executor_fractional.py` (new)

**Interfaces:**
- Produces: `_qty_str(q: float) -> str` (6 dp, trailing zeros stripped; `1.0 → '1'`, `0.123456789 → '0.123457'`).
- Produces: `_size_qty(side: str, notional: float, price: float, *, fractionable: bool) -> tuple[float, str | None]` — returns `(qty, skip_reason)`. Rules: `notional < DUST_FLOOR_USD or price <= 0 → (0.0, 'dust_below_1usd')`; `side == 'sell'` (short open) or `not fractionable` → `q = math.floor(notional/price)`, `q == 0 → (0.0, 'short_sub_share' | 'not_fractionable_sub_share')`; else `q = round(notional/price, 6)`, `q <= 0 → (0.0, 'dust_below_1usd')`.
- Produces: `_is_fractionable(ticker: str) -> bool` — reads `alpaca_tradable_universe.fractionable` once per process (dict cache; DB unreachable ⇒ `True` with one WARN log, matching the fail-open convention of `untradable_at_alpaca`).

- [ ] **Step 1: Write the failing tests** (`tests/execution/test_alpaca_executor_fractional.py`)

```python
import math
from execution import alpaca_executor as ae

def test_qty_str_formats_without_trailing_zeros():
    assert ae._qty_str(1.0) == '1' and ae._qty_str(0.5) == '0.5'
    assert ae._qty_str(0.1234567) == '0.123457' and ae._qty_str(12.000001) == '12.000001'

def test_size_qty_long_fractional_exact():
    assert ae._size_qty('buy', 6.0, 40.0, fractionable=True) == (0.15, None)

def test_size_qty_dust_below_1usd_skips():
    assert ae._size_qty('buy', 0.8, 40.0, fractionable=True) == (0.0, 'dust_below_1usd')

def test_size_qty_short_floors_and_never_rounds_up():
    assert ae._size_qty('sell', 36.0, 40.0, fractionable=True) == (0.0, 'short_sub_share')   # 0.9 sh
    assert ae._size_qty('sell', 104.0, 40.0, fractionable=True) == (2.0, None)                # 2.6 → 2

def test_size_qty_non_fractionable_long_floors():
    assert ae._size_qty('buy', 24.0, 40.0, fractionable=False) == (0.0, 'not_fractionable_sub_share')
    assert ae._size_qty('buy', 104.0, 40.0, fractionable=False) == (2.0, None)
```
Plus one test through `execute_single` with the existing `_mock_session`/`_mock_proc` fixtures from `tests/execution/test_alpaca_executor_cli.py` (copy the two helpers): a long order with `pct_nav` giving $6 on a $40 quote submits `--qty 0.15` and a short order giving 0.9 sh returns `status == 'SKIP'`, `reason == 'short_sub_share'`, with NO CLI call. Patch `ae._is_fractionable` to `lambda t: True`.

- [ ] **Step 2: Run to verify RED**: `python3 -m pytest -q tests/execution/test_alpaca_executor_fractional.py` → FAIL (`_qty_str`/`_size_qty` undefined).
- [ ] **Step 3: Implement** `_qty_str`, `_size_qty`, `_is_fractionable` near `_fetch_position_qty` (~L2256); replace the two `max(1, int(...))` sites with `qty, skip = _size_qty(...)`; on `skip` return the standard SKIP dict (`{'ticker', 'status': 'SKIP', 'reason': skip, 'client_order_id': coid}`) and `log(f'  SKIP {ticker}: {skip} (target ${notional:,.2f} @ {price:.2f})')`; `str(qty)` → `_qty_str(qty)` in `_submit_order_via_cli` (`'--qty', _qty_str(qty)`), the three `int(...)` casts → `float(...)`, `qty_opg < 1` → `qty_opg <= 0`.
- [ ] **Step 4: Run GREEN**: the new file + `tests/execution/test_alpaca_executor_cli.py tests/execution/test_alpaca_executor_bracket_recompute.py tests/execution/test_alpaca_executor_ext_hours.py tests/execution/test_alpaca_executor_dtbp_guard.py` → all pass.
- [ ] **Step 5: Commit** `feat(executor): fractional long sizing, floored shorts, dust skip (_size_qty)`.

### Task 2: Partial reduce by decimal `--qty`

**Files:**
- Modify: `src/execution/alpaca_executor.py` `_rth_position_close` (~L2294–2345: the `--percentage` branch) and the second `--percentage` site (~L1440, crypto path — LEAVE AS IS, crypto is already fractional; only the equity helper changes).
- Test: `tests/execution/test_alpaca_executor_fractional.py` (append)

**Interfaces:**
- Consumes: `_fetch_position_qty(ticker) -> float` (signed broker qty), `_qty_str`.
- Produces: `_reduce_qty(pos_qty: float, notional_oc: float, current_oc: float, *, is_short: bool) -> float` — pure: `q = abs(pos_qty) * notional_oc / current_oc`; short → `math.floor(q)`; long → `round(q, 6)`; capped at `abs(pos_qty)`.

- [ ] **Step 1: Failing tests**
```python
def test_reduce_qty_long_is_exact_fraction():
    assert ae._reduce_qty(3.0, 40.0, 120.0, is_short=False) == 1.0
    assert ae._reduce_qty(0.5, 10.0, 25.0, is_short=False) == 0.2

def test_reduce_qty_short_floors():
    assert ae._reduce_qty(-1.0, 70.0, 100.0, is_short=True) == 0.0
    assert ae._reduce_qty(-5.0, 55.0, 100.0, is_short=True) == 2.0
```
Plus through `_rth_position_close` (stub `_run_alpaca_cli` to capture args, `_fetch_position_qty` → `-1.0`, `_cancel_open_equity_orders` → no-op): a 70 % reduce of a 1-share short returns `status 'SKIP', reason 'reduce_rounds_to_zero'` and makes NO CLI call; a long 3-share position reduced to $80 of $120 calls `position close --symbol-or-asset-id T --qty 1`; a full close (notional ≥ 0.999·current) calls `position close` with neither `--qty` nor `--percentage`.

- [ ] **Step 2: RED** → `_reduce_qty` undefined.
- [ ] **Step 3: Implement**: in `_rth_position_close`, replace the `--percentage` computation with `pos_qty = _fetch_position_qty(ticker)`; `is_short = pos_qty < 0`; `q = _reduce_qty(...)`; `q <= 0 → return ({'status': 'SKIP', 'reason': 'reduce_rounds_to_zero', ...}, None)` before any cancel/close; else `cli_args_oc += ['--qty', _qty_str(q)]`. Keep the full-close branch unchanged. Fix the `entry_approx`/`qty_co_r` bookkeeping to use `float(...)` (there is an operator hunk `qty_co_r = abs(float(...))` on main's dirty tree — do not fight it; write the same expression).
- [ ] **Step 4: GREEN** on the Task 1 file list.
- [ ] **Step 5: Commit** `feat(executor): partial reduces by decimal --qty; short reduces floor, zero → skip`.

### Task 3: stop_reattach decimal quantities + DAY TIF for fractional positions

**Files:**
- Modify: `src/execution/stop_reattach.py` (`submit_protective_stop` ~L460–490 `str(int(qty))` ×3; `_oco_order_body` ~L541–559 `'qty': str(int(qty))`, `'time_in_force': 'gtc'`; `submit_protective_oco` ~L594)
- Test: `tests/execution/test_stop_reattach_fractional.py` (new)

**Interfaces:**
- Produces: `_qty_str(q: float) -> str` (same contract as Task 1; stop_reattach must not import alpaca_executor — duplicate the 5-line helper), `_is_fractional_qty(q: float) -> bool` (`abs(q - round(q)) > 1e-9`), `_tif_for_qty(q: float) -> str` → `'day'` if fractional else `'gtc'`.
- Behaviour: in `run_oco_reattach` (~L1030–1077) a fractional `pos_qty` SKIPS the `submit_protective_oco` call (Alpaca: `fractional orders must have order_class=simple`) and goes straight to `submit_protective_stop` with `tif=_tif_for_qty`; log `  ↓ {sym}: fractional position → simple DAY stop (no OCO)`. Whole-share positions unchanged (OCO/GTC).

- [ ] **Step 1: Failing tests**
```python
from execution import stop_reattach as sr

def test_tif_for_qty():
    assert sr._tif_for_qty(3.0) == 'gtc' and sr._tif_for_qty(0.4) == 'day'

def test_run_oco_reattach_fractional_uses_stop_not_oco(monkeypatch):
    # build a minimal positions list + stubs so run_oco_reattach reaches the submit branch (read the
    # existing tests in tests/execution/test_stop_reattach_coverage.py for the stub set); assert
    # submit_protective_oco is NOT called and submit_protective_stop is called with qty 0.4 and tif 'day'
    ...

def test_oco_body_whole_share_stays_gtc():
    b = sr._oco_order_body('F', 'sell', 3.0, 12.0, 10.0, 'c1')
    assert b['qty'] == '3' and b['time_in_force'] == 'gtc'

def test_protective_stop_fractional_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(sr, '_run_cli', lambda args, timeout=15: (seen.setdefault('args', args) and True, {'id': 'x'}, None))  # real shape: (ok, payload, err)
    sr.submit_protective_stop(ticker='F', position_side='long', qty=0.4, stop_price=10.0, dry_run=False)
    a = seen['args']; assert a[a.index('--qty') + 1] == '0.4' and a[a.index('--time-in-force') + 1] == 'day'
```
(`_run_cli` returns `(ok: bool, payload: dict|str|None, err: dict|None)`.)
- [ ] **Step 2: RED**.
- [ ] **Step 3: Implement**; the `--time-in-force` in `submit_protective_stop` becomes `_tif_for_qty(qty)`; log lines print `_qty_str(qty)`.
- [ ] **Step 4: GREEN** on the new file + `tests/execution/test_stop_reattach_coverage.py tests/execution/test_stop_reattach_emergency_exit.py tests/execution/test_stop_reattach_pagination.py`.
- [ ] **Step 5: Commit** `feat(stop_reattach): decimal qty, DAY tif for fractional positions`.

### Task 4: Fractional opens are simple DAY orders (no bracket)

**Files:**
- Modify: `src/execution/alpaca_executor.py` `execute_single` open path (~L2690–2700: the `if side == 'sell' and order_class == 'bracket'` drop-to-simple block)
- Test: `tests/execution/test_alpaca_executor_fractional.py` (append)

**Interfaces:**
- Consumes: `_size_qty`, `_qty_str` (Task 1); `_is_fractional_qty(q: float) -> bool` (added to alpaca_executor by Task 2 for the OPG guard; same 1-line contract as Task 3's copy).

- [ ] **Step 1: Failing test**: through `execute_single` with the cli-test fixtures: a LONG whose qty is fractional (0.15 sh) submits with `--order-class` ABSENT (simple), `--time-in-force day`, `--qty 0.15`, and the log contains `fractional → simple order_class`; a LONG with qty 3 (whole) still submits `--order-class bracket` with `--take-profit`/`--stop-loss`.
- [ ] **Step 2: RED**.
- [ ] **Step 3: Implement**: after the short drop-to-simple block add `if order_class == 'bracket' and _is_fractional_qty(qty): log(f'  ↓ {ticker}: fractional → simple order_class (Alpaca: fractional orders must have order_class=simple)'); order_class = 'simple'`. `tif` is already `'day'` on the RTH path.
- [ ] **Step 4: GREEN** on the Task 1 file list.
- [ ] **Step 5: Commit** `feat(executor): fractional opens submit as simple DAY orders`.

### Task 5: Docs + runbook
- Modify: `docs/archive/changelog.md` (entry), `docs/superpowers/specs/2026-11-23-fractional-shares-design.md` (§7 probe result, §9 as-built), `/root/CLAUDE.md` is the controller's (not the implementer's).
- Note the one protection gap (no overnight broker stop on fractional positions; DAY stop re-armed each session; circuit/account breakers cover intraday).
- [ ] Commit `docs: fractional shares as-built`.
