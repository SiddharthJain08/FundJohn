# Faithful Bracket Recording + Latest-Run Reattach + After-Hours TPs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make protective brackets survive correctly — record the legs the broker actually accepted, re-establish TP/stop from the latest run's real orders without ever silently dropping a take-profit, and let take-profits rest during extended hours.

**Architecture:** Three gated, independent changes. **W1** fixes `record_submission` to source stop/target from the submitted (placed) legs in the execute result instead of the pre-submit `order` dict. **W2** teaches `stop_reattach` to read the most-recent terminal bracket's real leg prices from Alpaca order history (DB fallback) and to surface — never silently drop — a missing take-profit. **W3** adds a new `afterhours_tp` module that places resting `limit/day/extended_hours` take-profits at each ext-hours session open plus a session-boundary reconcile. Each behind a default-OFF env gate.

**Tech Stack:** Python 3.13, `psycopg2`, Alpaca CLI (`/root/go/bin/alpaca`), Alpaca REST (`requests`), systemd timers, pytest.

**Spec:** `docs/superpowers/specs/2026-06-16-faithful-bracket-recording-reattach-afterhours-tp-design.md`

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/execution/alpaca_executor.py` | order submit + `record_submission` | Modify: W1 source legs from result; expose `_submit_order_via_cli` import surface (already module-level) |
| `src/execution/stop_reattach.py` | overnight reattach (OCO + bare-stop floor) | Modify: W2 broker-leg reader + never-silent-drop |
| `src/execution/afterhours_tp.py` | ext-hours TP placement + session reconcile | Create: W3 |
| `tests/test_record_placed_bracket.py` | W1 unit tests | Create |
| `tests/test_reattach_from_broker.py` | W2 unit tests | Create |
| `tests/test_afterhours_tp.py` | W3 unit tests | Create |
| `docs/openclaw-afterhours-tp-premarket.{service,timer}` | 4:00 ET trigger | Create |
| `docs/openclaw-afterhours-tp-postmarket.{service,timer}` | 4:00 PM ET trigger | Create |

**Gates (default-OFF):** `OPENCLAW_RECORD_PLACED_BRACKET` (W1), `OPENCLAW_REATTACH_FROM_BROKER` (W2), `OPENCLAW_AFTERHOURS_TP` (W3).

**Test invocation:** from the worktree root, `python3 -m pytest <path> -q`. Tests insert `src` on `sys.path` (mirror `tests/test_protective_oco.py`). Keep suites sequential (2-core VPS).

---

## Phase 1 — W1: Record the placed bracket

Context: `execute_single` returns `{'entry': entry, 'stop': stop, 'target': target, ...}` where `stop`/`target` are the **post-recompute legs actually submitted** (`alpaca_executor.py:~2497`). But `record_submission` (`:349`) reads `entry` from the result yet `stop`/`target` from the pre-submit `order` dict (`:391-392`), so the audit row is stale-by-construction. The fix: prefer the result's `stop`/`target`, fall back to `order`.

### Task 1: `record_submission` sources stop/target from the placed legs

**Files:**
- Modify: `src/execution/alpaca_executor.py:349-399` (`record_submission`)
- Test: `tests/test_record_placed_bracket.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""W1: record_submission persists the legs actually submitted (from the
execute result), not the pre-submit per-strategy order dict."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import alpaca_executor as ax


class _Cur:
    def __init__(self): self.params = None
    def execute(self, sql, params): self.params = params
    def close(self): pass


class _Conn:
    def __init__(self): self.cur = _Cur()
    def cursor(self): return self.cur


def _row(conn):
    # INSERT param order (record_submission): run_date, ticker, strategy_id,
    # direction, qty, entry_price, stop_price, target_price, ...
    p = conn.cur.params
    return {'entry': p[5], 'stop': p[6], 'target': p[7]}


def test_records_placed_legs_over_degenerate_order(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    conn = _Conn()
    # order carries the stale per-strategy levels (WDC-style: target below entry)
    order = {'ticker': 'WDC', 'strategy_id': 'momentum_12_1', 'direction': 'long',
             'entry': 627.51, 'stop': 516.11, 't1': 604.79, 'pct_nav': 0.02}
    # result carries the legs actually submitted (post-recompute / stacked)
    result = {'status': 'submitted', 'qty': 46, 'notional': 28878.8,
              'order_id': 'oid1', 'http': None, 'reason': None,
              'entry': 627.80, 'stop': 611.89, 'target': 717.03}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    r = _row(conn)
    assert abs(r['target'] - 717.03) < 1e-6   # placed TP, not 604.79
    assert abs(r['stop'] - 611.89) < 1e-6      # placed stop, not 516.11
    assert abs(r['entry'] - 627.80) < 1e-6


def test_falls_back_to_order_when_no_placed_legs(monkeypatch):
    # dtbp-skip path: resp has no stop/target → record the intended order levels
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    conn = _Conn()
    order = {'ticker': 'AAPL', 'strategy_id': 's', 'direction': 'long',
             'entry': 100.0, 'stop': 95.0, 't1': 110.0, 'pct_nav': 0.01}
    resp = {'status': 'skipped_dtbp', 'qty': 0, 'order_id': None, 'entry': 100.0}
    ax.record_submission(conn, '2026-06-15', order, resp, 'day', 'simple', 'coid2')
    r = _row(conn)
    assert abs(r['stop'] - 95.0) < 1e-6
    assert abs(r['target'] - 110.0) < 1e-6


def test_gate_off_is_legacy_behavior(monkeypatch):
    monkeypatch.delenv('OPENCLAW_RECORD_PLACED_BRACKET', raising=False)
    conn = _Conn()
    order = {'ticker': 'WDC', 'strategy_id': 's', 'direction': 'long',
             'entry': 627.51, 'stop': 516.11, 't1': 604.79, 'pct_nav': 0.02}
    result = {'status': 'submitted', 'qty': 46, 'order_id': 'oid1',
              'entry': 627.80, 'stop': 611.89, 'target': 717.03}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    r = _row(conn)
    assert abs(r['target'] - 604.79) < 1e-6   # legacy: reads order['t1']
    assert abs(r['stop'] - 516.11) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_record_placed_bracket.py -q`
Expected: FAIL (`test_records_placed_legs_over_degenerate_order` asserts 717.03 but legacy records 604.79).

- [ ] **Step 3: Add the gate helper near the other gate helpers (top of `alpaca_executor.py`, beside existing `os.environ` gates)**

```python
def _record_placed_bracket_on() -> bool:
    """W1: when ON, alpaca_submissions records the legs actually submitted
    (from the execute result) rather than the pre-submit per-strategy order."""
    return os.environ.get('OPENCLAW_RECORD_PLACED_BRACKET') == '1'
```

- [ ] **Step 4: Change the stop/target params in `record_submission` (`alpaca_executor.py:391-392`)**

Replace:
```python
        order.get('stop') or 0.0,
        order.get('t1') or order.get('target') or 0.0,
```
with:
```python
        _placed_or_order_stop(order, alpaca_resp),
        _placed_or_order_target(order, alpaca_resp),
```

And add these helpers immediately above `record_submission`:
```python
def _placed_or_order_stop(order, resp) -> float:
    """Prefer the stop actually submitted (in the execute result) over the
    pre-submit order's stop, when W1 is ON. The result's `stop` is the
    post-recompute/stacked leg sent to the broker."""
    if _record_placed_bracket_on():
        s = (resp or {}).get('stop')
        if s is not None:
            return float(s)
    return float(order.get('stop') or 0.0)


def _placed_or_order_target(order, resp) -> float:
    """Prefer the take-profit actually submitted over the pre-submit order's.
    Logs a WARN if the chosen target is degenerate (<= entry for a long /
    >= entry for a short) so a bad placement is never recorded silently."""
    tgt = None
    if _record_placed_bracket_on():
        t = (resp or {}).get('target')
        if t is not None:
            tgt = float(t)
    if tgt is None:
        tgt = float(order.get('t1') or order.get('target') or 0.0)
    entry = float((resp or {}).get('entry') or order.get('entry') or 0.0)
    side = (order.get('direction') or 'long').lower()
    if entry > 0 and tgt > 0:
        degenerate = (tgt <= entry) if side != 'short' else (tgt >= entry)
        if degenerate:
            log(f"  ⚠ {order.get('ticker','?')}: recording DEGENERATE target "
                f"{tgt:.2f} vs entry {entry:.2f} (side={side}) — placement bug upstream")
    return tgt
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_record_placed_bracket.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the executor regression suite**

Run: `python3 -m pytest tests/test_protective_oco.py tests/test_bracket_stacking.py -q`
Expected: PASS (26 tests, unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/execution/alpaca_executor.py tests/test_record_placed_bracket.py
git commit -m "feat(W1): record the placed bracket legs, not the pre-submit order

record_submission now sources stop/target from the execute result (the legs
actually submitted to Alpaca) when OPENCLAW_RECORD_PLACED_BRACKET=1, falling
back to the order dict. Degenerate recorded targets are logged, never silent.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — W2: Reattach from the latest run; never drop a TP

Context: `stop_reattach.run_oco_reattach` (`:434`) recomputes TP/stop from the latest DB `alpaca_submissions` row via `_compute_new_stop`/`_compute_new_target`, and on a `degenerate`/`reached` target it skips the OCO so only the bare-stop floor fires — silently. W2 adds a broker-history source and a surfaced never-drop path.

### Task 2: `latest_broker_bracket` — read the last placed bracket's legs

**Files:**
- Modify: `src/execution/stop_reattach.py` (add reader near `latest_stop_submission`)
- Test: `tests/test_reattach_from_broker.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""W2: read the most-recent terminal bracket's real leg prices from Alpaca
order history, preferred over the DB submission row."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import stop_reattach as sr


# A nested order-list payload like `alpaca order list --status all --nested`.
_WDC_ORDERS = [
    {'symbol': 'WDC', 'side': 'buy', 'qty': '46', 'order_class': 'bracket',
     'submitted_at': '2026-06-15T14:02:52Z', 'status': 'filled', 'type': 'market',
     'legs': [
         {'symbol': 'WDC', 'side': 'sell', 'type': 'limit', 'limit_price': '717.03',
          'stop_price': None, 'status': 'expired'},
         {'symbol': 'WDC', 'side': 'sell', 'type': 'stop', 'limit_price': None,
          'stop_price': '611.89', 'status': 'canceled'},
     ]},
    {'symbol': 'WDC', 'side': 'buy', 'qty': '28', 'order_class': 'bracket',
     'submitted_at': '2026-06-12T14:02:25Z', 'status': 'filled', 'type': 'market',
     'legs': [
         {'symbol': 'WDC', 'side': 'sell', 'type': 'limit', 'limit_price': '716.52',
          'stop_price': None, 'status': 'canceled'},
         {'symbol': 'WDC', 'side': 'sell', 'type': 'stop', 'limit_price': None,
          'stop_price': '563.18', 'status': 'filled'},
     ]},
]


def test_latest_broker_bracket_picks_most_recent_long(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, _WDC_ORDERS, None))
    b = sr.latest_broker_bracket('WDC', 'long')
    assert b is not None
    assert abs(b['target'] - 717.03) < 1e-6   # TP leg of the 06-15 bracket
    assert abs(b['stop'] - 611.89) < 1e-6      # stop leg of the 06-15 bracket


def test_latest_broker_bracket_none_when_no_bracket(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, [], None))
    assert sr.latest_broker_bracket('WDC', 'long') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reattach_from_broker.py -q`
Expected: FAIL (`AttributeError: module 'execution.stop_reattach' has no attribute 'latest_broker_bracket'`).

- [ ] **Step 3: Implement `latest_broker_bracket` in `stop_reattach.py` (after `latest_stop_submission`)**

```python
def latest_broker_bracket(ticker: str, position_side: str) -> dict | None:
    """Most-recent terminal bracket order for `ticker` on the side that OPENED
    `position_side`, with its real take-profit + stop leg prices read back from
    Alpaca order history. This is the levels the LAST run actually placed —
    independent of the alpaca_submissions audit row.

    Returns {'entry': None, 'stop': float, 'target': float, 'order_id': str}
    or None when no bracket with both legs is found."""
    open_side = 'buy' if position_side == 'long' else 'sell'
    ok, payload, err = _run_cli(['order', 'list', '--status', 'all',
                                 '--nested', '--limit', '500'])
    if not ok:
        log(f'order history fetch failed ({(err or {}).get("error","unknown")})')
        return None
    cands = []
    for o in (payload or []):
        if o.get('symbol') != ticker or o.get('side') != open_side:
            continue
        if (o.get('order_class') or '') != 'bracket':
            continue
        tp = stp = None
        for leg in (o.get('legs') or []):
            ltype = (leg.get('type') or leg.get('order_type') or '').lower()
            if ltype == 'limit' and leg.get('limit_price'):
                tp = float(leg['limit_price'])
            elif ltype in ('stop', 'stop_limit') and leg.get('stop_price'):
                stp = float(leg['stop_price'])
        if tp and stp:
            cands.append((o.get('submitted_at') or '', tp, stp, o.get('id') or ''))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])           # ascending submitted_at
    _, tp, stp, oid = cands[-1]              # most recent
    return {'entry': None, 'stop': stp, 'target': tp, 'order_id': oid}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_reattach_from_broker.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/execution/stop_reattach.py tests/test_reattach_from_broker.py
git commit -m "feat(W2): read the latest placed bracket's legs from Alpaca history

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 3: Prefer broker-bracket legs in `run_oco_reattach`; never silent-drop

**Files:**
- Modify: `src/execution/stop_reattach.py` (`run_oco_reattach` `:434-497`, `main` surface section `:607-611`)
- Test: `tests/test_reattach_from_broker.py` (extend)

- [ ] **Step 1: Write the failing tests (append to the file)**

```python
def _pos(side, avg, cur, qty=46, sym='WDC'):
    return {'symbol': sym, 'side': side, 'avg_entry_price': avg,
            'current_price': cur, 'qty': qty}


def test_reattach_uses_broker_target_not_degenerate_db(monkeypatch):
    """WDC replay: DB row target is degenerate (604.79<627.51) but the broker
    bracket's real TP (717.03) is recovered → OCO is placed, not dropped."""
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket',
                        lambda t, s: {'entry': None, 'stop': 611.89,
                                      'target': 717.03, 'order_id': 'oid'})
    # DB row is the degenerate one; it must NOT be used when broker legs exist
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    monkeypatch.setattr(sr, 'cancel_stops_for', lambda s, d: 0)
    placed = {}
    def _fake_oco(*, ticker, position_side, qty, stop_price, target_price, dry_run):
        placed.update(dict(target=target_price, stop=stop_price))
        return {'ticker': ticker, 'status': 'submitted'}
    monkeypatch.setattr(sr, 'submit_protective_oco', _fake_oco)
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=False)
    assert stats['oco'] == 1
    # target re-anchored to current avg using broker pct (717.03/627.51-ish > current)
    assert placed['target'] > 694.0           # valid profit-side TP, not dropped


def test_no_silent_drop_when_target_unavailable(monkeypatch):
    """No broker legs and degenerate DB → place the stop AND record tp_missing;
    NOT a silent bare-stop-only skip."""
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket', lambda t, s: None)
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=True)
    assert stats['tp_missing'] == 1           # surfaced, not silently skipped
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_reattach_from_broker.py -q`
Expected: FAIL (`run_oco_reattach` doesn't consult `latest_broker_bracket`; no `tp_missing` key).

- [ ] **Step 3: Add a gate helper + a source-resolver in `stop_reattach.py`**

```python
def _reattach_from_broker_on() -> bool:
    return os.environ.get('OPENCLAW_REATTACH_FROM_BROKER') == '1'


def _resolve_intended_bracket(conn, ticker: str, side: str):
    """Return a submission-shaped dict {'entry_price','stop_price','target_price'}
    representing the latest run's intended bracket. Prefers the broker's
    last-placed bracket legs (W2); falls back to the DB submission row.
    `entry_price` from the broker path is taken from the DB row (the broker leg
    carries no entry) so the existing pct re-anchor math is unchanged."""
    if _reattach_from_broker_on():
        b = latest_broker_bracket(ticker, side)
        if b:
            db = latest_stop_submission(conn, ticker, side) or {}
            entry = b.get('entry') or db.get('entry_price')
            if entry:
                return {'entry_price': float(entry),
                        'stop_price': b['stop'], 'target_price': b['target']}
    return latest_stop_submission(conn, ticker, side)
```

- [ ] **Step 4: Wire it into `run_oco_reattach` and add `tp_missing`**

In `run_oco_reattach` (`:454`), replace `sub = latest_stop_submission(conn, sym, side)` with:
```python
        sub = _resolve_intended_bracket(conn, sym, side)
```
Add `'tp_missing': 0` to the `stats` dict initializer (`:440-441`). Then change the target-skip branch (`:466-470`) from:
```python
        if tstatus != 'ok':
            stats['reached' if tstatus == 'reached' else 'degenerate'] += 1
            continue
```
to:
```python
        if tstatus != 'ok':
            # Never silently drop the TP: leave the loss side to the bare-stop
            # floor, but surface the missing take-profit for operator review.
            if tstatus == 'reached':
                stats['reached'] += 1
            else:
                stats['degenerate'] += 1
                stats['tp_missing'] += 1
                log(f'  ⚠ {sym}: no valid take-profit from broker history or DB '
                    f'(tstatus={tstatus}) — bare stop only, TP NOT re-established')
            continue
```

- [ ] **Step 5: Surface `tp_missing` in `main` (after the `breached` block, `:607-611`)**

```python
        if oco_stats.get('tp_missing'):
            log(f'⚠ {oco_stats["tp_missing"]} position(s) re-stopped WITHOUT a '
                f'take-profit (no recoverable TP) — operator review needed')
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_reattach_from_broker.py tests/test_protective_oco.py -q`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add src/execution/stop_reattach.py tests/test_reattach_from_broker.py
git commit -m "feat(W2): reattach from latest placed bracket; never silently drop a TP

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — W3: After-hours take-profits (no monitor)

Context: native stop/OCO are RTH-only. Place resting `limit/day/extended_hours=true` sells at the TP at each ext-hours session open; reconcile the unlinked RTH GTC stop at session boundaries. Reuses `alpaca_executor._submit_order_via_cli(order_type='limit', extended_hours=True)`.

### Task 4: `afterhours_tp` module — desired-TP computation + placement

**Files:**
- Create: `src/execution/afterhours_tp.py`
- Test: `tests/test_afterhours_tp.py`

- [ ] **Step 1: Write the failing test**

```python
"""W3: extended-hours take-profit placement (limit/day/extended_hours)."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import afterhours_tp as ah


def test_desired_tps_long_and_short():
    positions = [
        {'symbol': 'WDC', 'side': 'long', 'qty': '46'},
        {'symbol': 'MU', 'side': 'short', 'qty': '12'},
        {'symbol': 'NOTP', 'side': 'long', 'qty': '5'},
    ]
    def lookup(sym, side):
        return {'WDC': {'target': 717.03}, 'MU': {'target': 880.0}}.get(sym)
    out = {d['ticker']: d for d in ah.desired_tps(positions, lookup)}
    assert out['WDC']['side'] == 'sell' and abs(out['WDC']['tp'] - 717.03) < 1e-6
    assert out['MU']['side'] == 'buy' and abs(out['MU']['tp'] - 880.0) < 1e-6
    assert 'NOTP' not in out            # no known TP → no order


def test_already_covered_qty_is_skipped():
    positions = [{'symbol': 'WDC', 'side': 'long', 'qty': '46'}]
    lookup = lambda s, side: {'target': 717.03}
    out = ah.desired_tps(positions, lookup, tp_covered={'WDC': 46})
    assert out == []                    # already has a resting limit for full qty
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_afterhours_tp.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Create `src/execution/afterhours_tp.py` with the pure planner + gate**

```python
#!/usr/bin/env python3
"""afterhours_tp.py — W3: resting extended-hours take-profits.

Alpaca extended-hours orders must be limit + day TIF + extended_hours=true.
A sell-limit above market (buy-limit below, for shorts) is a clean ext-hours
take-profit. Stops cannot be represented in ext-hours, so this covers UPSIDE
only; the RTH GTC stop still covers downside.

Placed at each ext-hours session open; a session-boundary reconcile cancels the
prior session's TP and resizes the (unlinked) GTC stop after an ext-hours fill.

Gate: OPENCLAW_AFTERHOURS_TP (default OFF).
"""
from __future__ import annotations
import os


def afterhours_tp_on() -> bool:
    return os.environ.get('OPENCLAW_AFTERHOURS_TP') == '1'


def desired_tps(positions, bracket_lookup, tp_covered=None):
    """Pure: map open positions to the ext-hours TP orders to place.

    positions: [{'symbol','side','qty'}]
    bracket_lookup(symbol, side) -> {'target': float} | None  (latest placed TP)
    tp_covered: {symbol: qty already resting on a limit} (skip covered qty)
    Returns [{'ticker','side','qty','tp'}] — side is the EXIT side.
    """
    tp_covered = tp_covered or {}
    out = []
    for p in positions:
        sym = p.get('symbol')
        side = (p.get('side') or '').lower()
        try:
            qty = abs(float(p.get('qty') or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0 or side not in ('long', 'short'):
            continue
        if tp_covered.get(sym, 0.0) >= qty - 0.01:
            continue
        b = bracket_lookup(sym, side)
        tp = (b or {}).get('target')
        if not tp or float(tp) <= 0:
            continue
        exit_side = 'sell' if side == 'long' else 'buy'
        out.append({'ticker': sym, 'side': exit_side, 'qty': int(qty),
                    'tp': float(tp)})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_afterhours_tp.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/execution/afterhours_tp.py tests/test_afterhours_tp.py
git commit -m "feat(W3): after-hours TP planner (desired_tps)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5: Placement + session-boundary reconcile driver

**Files:**
- Modify: `src/execution/afterhours_tp.py` (add `place_afterhours_tps`, `reconcile_afterhours`, `main`)
- Test: `tests/test_afterhours_tp.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_place_submits_extended_hours_limit(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    calls = []
    def _fake_submit(**kw):
        calls.append(kw)
        return True, {'id': 'oid'}, None
    monkeypatch.setattr(ah, '_submit_limit', _fake_submit)
    plan = [{'ticker': 'WDC', 'side': 'sell', 'qty': 46, 'tp': 717.03}]
    n = ah._place_plan(plan, dry_run=False)
    assert n == 1
    kw = calls[0]
    assert kw['order_type'] == 'limit' and kw['extended_hours'] is True
    assert kw['tif'] == 'day' and kw['order_class'] == 'simple'
    assert abs(kw['limit_price'] - 717.03) < 1e-6


def test_gate_off_places_nothing(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_TP', raising=False)
    assert ah._place_plan([{'ticker': 'WDC', 'side': 'sell', 'qty': 46,
                            'tp': 717.03}], dry_run=False) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_afterhours_tp.py -q`
Expected: FAIL (`_place_plan`/`_submit_limit` undefined).

- [ ] **Step 3: Add placement, reconcile, and `main` to `afterhours_tp.py`**

```python
import json, subprocess, sys
from datetime import datetime

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [AFTERHOURS_TP] {msg}")


def _submit_limit(*, ticker, side, qty, limit_price, tif, order_class,
                  order_type, extended_hours, coid):
    """Thin wrapper over the executor's CLI submit (kept patchable in tests)."""
    from execution.alpaca_executor import _submit_order_via_cli
    return _submit_order_via_cli(
        ticker=ticker, side=side, qty=qty, tif=tif, order_class=order_class,
        target=None, stop=None, coid=coid, order_type=order_type,
        extended_hours=extended_hours, limit_price=limit_price)


def _place_plan(plan, dry_run: bool) -> int:
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return 0
    n = 0
    for o in plan:
        coid = f"ahtp_{o['ticker']}_{int(datetime.utcnow().timestamp())}"
        if dry_run:
            log(f"  DRY-RUN {o['ticker']} {o['side'].upper()} LIMIT x{o['qty']} "
                f"@ {o['tp']:.2f} ext_hours=True")
            n += 1
            continue
        ok, _pay, err = _submit_limit(
            ticker=o['ticker'], side=o['side'], qty=o['qty'],
            limit_price=o['tp'], tif='day', order_class='simple',
            order_type='limit', extended_hours=True, coid=coid)
        if ok:
            log(f"  ✔ {o['ticker']} ext-hours TP x{o['qty']} @ {o['tp']:.2f}")
            n += 1
        else:
            log(f"  ✗ {o['ticker']} ext-hours TP failed: {(err or {}).get('error','?')}")
    return n
```

For the reconcile (cancel prior ext-hours TPs by coid prefix `ahtp_`, and resize the GTC stop if a TP filled) and `main`, reuse `stop_reattach._run_cli`, `fetch_positions`, `fetch_tp_covered`, `cancel_stops_for`, `latest_broker_bracket`, and `submit_protective_stop`:

```python
def _cli(args, timeout=15):
    from execution.stop_reattach import _run_cli
    return _run_cli(args, timeout=timeout)


def reconcile_afterhours(dry_run: bool) -> dict:
    """Cancel any resting ext-hours TP we placed (coid prefix 'ahtp_'); if a
    position's TP filled (qty shrank below the GTC stop's qty), resize the stop.
    Idempotent."""
    from execution.stop_reattach import fetch_positions
    stats = {'tp_canceled': 0, 'stops_resized': 0}
    ok, orders, _ = _cli(['order', 'list', '--status', 'open'])
    if not ok:
        return stats
    pos_qty = {p['symbol']: abs(float(p.get('qty') or 0))
               for p in fetch_positions()}
    for o in (orders or []):
        if (o.get('client_order_id') or '').startswith('ahtp_'):
            if dry_run:
                log(f"  DRY-RUN cancel ext-hours TP {o.get('symbol')} {o.get('id')}")
            else:
                _cli(['order', 'cancel', '--order-id', o.get('id')])
            stats['tp_canceled'] += 1
    # Resize over-covered GTC stops (a filled ext-hours TP shrank the position).
    for o in (orders or []):
        if (o.get('type') or o.get('order_type')) not in ('stop', 'stop_limit'):
            continue
        sym = o.get('symbol')
        try:
            stop_qty = abs(float(o.get('qty') or 0))
        except (TypeError, ValueError):
            stop_qty = 0.0
        held = pos_qty.get(sym, 0.0)
        if stop_qty > held + 0.01:
            log(f"  ⚠ {sym}: stop qty {stop_qty:.0f} > held {held:.0f} "
                f"(ext-hours TP filled) — cancel+resize")
            if not dry_run:
                _cli(['order', 'cancel', '--order-id', o.get('id')])
                # leave re-establishment of a correctly-sized stop to the next
                # stop_reattach pass (single source of stop-sizing truth)
            stats['stops_resized'] += 1
    return stats


def main(argv=None) -> int:
    import argparse
    from execution.stop_reattach import fetch_positions, fetch_tp_covered
    ap = argparse.ArgumentParser()
    ap.add_argument('--reconcile', action='store_true',
                    help='session-boundary reconcile instead of placement')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return 0
    if args.reconcile:
        log(f'reconcile: {reconcile_afterhours(args.dry_run)}')
        return 0
    from execution.stop_reattach import latest_broker_bracket
    positions = [p for p in fetch_positions()]
    plan = desired_tps(positions, latest_broker_bracket,
                       tp_covered=fetch_tp_covered())
    log(f'placing {len(plan)} ext-hours TP(s)')
    _place_plan(plan, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_afterhours_tp.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/execution/afterhours_tp.py tests/test_afterhours_tp.py
git commit -m "feat(W3): ext-hours TP placement + session-boundary reconcile

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 6: systemd timers for the two session boundaries

**Files:**
- Create: `docs/openclaw-afterhours-tp-premarket.service`, `docs/openclaw-afterhours-tp-premarket.timer`
- Create: `docs/openclaw-afterhours-tp-postmarket.service`, `docs/openclaw-afterhours-tp-postmarket.timer`

- [ ] **Step 1: Write the premarket service** (`docs/openclaw-afterhours-tp-premarket.service`)

```ini
[Unit]
Description=Place resting extended-hours take-profits at the premarket open (4:00 ET)
After=network-online.target redis.service postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 /root/openclaw/src/execution/afterhours_tp.py
StandardOutput=append:/var/log/openclaw-afterhours-tp.log
StandardError=append:/var/log/openclaw-afterhours-tp.log
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Write the premarket timer** (`docs/openclaw-afterhours-tp-premarket.timer`)

```ini
[Unit]
Description=Fire ext-hours TP placement Mon-Fri 04:00 America/New_York (premarket open)

[Timer]
OnCalendar=Mon..Fri *-*-* 04:00:00 America/New_York
Persistent=true
Unit=openclaw-afterhours-tp-premarket.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Write the postmarket service** (`docs/openclaw-afterhours-tp-postmarket.service`)

Same as premarket service but Description references the postmarket open and ExecStart runs placement (the 16:00 ET cycle replaces RTH OCO with ext-hours TPs after the close). Body identical to Step 1 except the `Description=`:
```ini
[Unit]
Description=Place resting extended-hours take-profits at the postmarket open (16:00 ET)
After=network-online.target redis.service postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 /root/openclaw/src/execution/afterhours_tp.py
StandardOutput=append:/var/log/openclaw-afterhours-tp.log
StandardError=append:/var/log/openclaw-afterhours-tp.log
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Write the postmarket timer** (`docs/openclaw-afterhours-tp-postmarket.timer`)

```ini
[Unit]
Description=Fire ext-hours TP placement Mon-Fri 16:05 America/New_York (postmarket open)

[Timer]
OnCalendar=Mon..Fri *-*-* 16:05:00 America/New_York
Persistent=true
Unit=openclaw-afterhours-tp-postmarket.service

[Install]
WantedBy=timers.target
```

Note: `America/New_York` in `OnCalendar` handles EST/EDT automatically. 16:05 (not 16:00) lets the RTH-close cleanup settle first. Reconcile runs are wired via `--reconcile` in the activation runbook (operator step), not a separate timer in v1.

- [ ] **Step 5: Commit**

```bash
git add docs/openclaw-afterhours-tp-*.service docs/openclaw-afterhours-tp-*.timer
git commit -m "feat(W3): systemd timers for premarket/postmarket ext-hours TP placement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 7: Live feasibility smoke (operator-run, documented)

**Files:** none (runbook step recorded in the spec's section 6).

- [ ] **Step 1:** Document in the activation runbook that, before flipping `OPENCLAW_AFTERHOURS_TP=1`, the operator runs a paper smoke during a pre/post-market window: submit one tiny `limit + extended_hours=true` sell on a held name (expect ACCEPTED) and one `stop + extended_hours=true` (expect REJECTED), confirming the limits-only design. This is a manual verification, not automated (it requires a live ext-hours session + a real position).

---

## Final: integration check

- [ ] **Step 1: Full affected-area regression**

Run: `python3 -m pytest tests/test_record_placed_bracket.py tests/test_reattach_from_broker.py tests/test_afterhours_tp.py tests/test_protective_oco.py tests/test_bracket_stacking.py -q`
Expected: PASS (all).

- [ ] **Step 2: Import-sanity for the new module under the live entrypoint**

Run: `cd /root/.config/superpowers/worktrees/bracket-recording-reattach-afterhours-tp && python3 -c "import sys; sys.path.insert(0,'src'); from execution import afterhours_tp, stop_reattach, alpaca_executor; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 3: Final commit if anything was amended**

```bash
git add -A && git commit -m "test: full affected-area regression green" --allow-empty
```

---

## Self-review

- **Spec coverage:** W1 §3 → Task 1. W2 §4 (broker-leg source) → Tasks 2–3; (never-drop) → Task 3 Steps 4–5. W3 §5 (placement) → Tasks 4–5; (reconcile) → Task 5; (timers) → Task 6; (live smoke) → Task 7. Gates §6 → Tasks 1/3/4. No schema change → honored (no migration task). ✓
- **Placeholders:** none — every code step shows complete code; the only manual step (Task 7) is explicitly a live operator action, not a code placeholder.
- **Type consistency:** `latest_broker_bracket(ticker, side) -> {'entry','stop','target','order_id'}` is used identically by `_resolve_intended_bracket` (Task 3) and `desired_tps`'s `bracket_lookup` (Task 4). `desired_tps` rows `{'ticker','side','qty','tp'}` match `_place_plan`'s consumption. `_submit_limit` kwargs match `_submit_order_via_cli`'s signature (`order_type`, `extended_hours`, `limit_price`, `tif`, `order_class`). ✓
- **Open items from spec §7:** the WDC provenance question is resolved (main path read `order` not the result); both `record_submission` callers are covered (the dtbp-skip path has no placed legs and correctly falls back). The submit-response-inlines-legs question is sidestepped — W2 reads legs from `order list --nested` (confirmed to roll up legs), and W1 records the result's already-known placed `target`/`stop`.
