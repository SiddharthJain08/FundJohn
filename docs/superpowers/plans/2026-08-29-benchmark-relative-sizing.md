# Benchmark-Relative Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the SPY benchmark from every strategy gate, make strategy weights raw sleeve Sharpes (no `√hold`, no trade factor), add a `S_beta_spy` beta sleeve, and size every alpha ticker on its tangency conviction **in excess of** SPY's regime Sharpe (`S_adj − S_m`), with beta uncapped.

**Architecture:** Four independent changes on the existing rails. (1) Delete the R1 leg from `regime_qualification` / `lifecycle` / both assigners / `promotion_service.js`. (2) `strategy_weights._regime_weight` stops dividing by `√hold`; the sizer stops multiplying by the trade factor — both behind revert flags. (3) A plain fleet strategy flagged `benchmark_sleeve=True` (class attr; mirrored into `strategy_registry.parameters`) supplies beta; the sizer reads that flag from the registry to exempt benchmark tickers from the acting gate, the hurdle, and both caps. (4) One pure function `apply_benchmark_hurdle` inserted at the single line where the sizer rebuilds `ticker_w` from S_adj, flag-gated with a per-cycle shadow line and a read-only replay script as the parity artefact.

**Tech Stack:** Python 3 (pandas, pyarrow, psycopg2, pytest), Node (node --test), PostgreSQL, systemd transient units.

**Spec:** `docs/specs/2026-08-29-benchmark-relative-sizing-spec.md` (commit `d77534e`). Executors read both.

## Global Constraints

- Production tree = `/root/openclaw` on `main`. One commit per task, `git push origin main` after each. **Never stage** `src/strategies/manifest.json`, `src/strategies/strategy_signatures*.json`, or the foreign uncommitted edit in `src/engine/daily-health-digest.js`.
- Tests reach the REAL DB (`.env` loads at import). Run **only the tests named in each task** — never the full suite while the fleet runs. Fixture tickers must be synthetic (`ZZT*`, `T00`…); `AAA` is a real ETF.
- No heavy compute 13:00–20:15 UTC. Backtests run as transient systemd units (`systemd-run`, `Nice=19`, `CPUQuota=100%`, `MemoryMax=3500M`). Never `source .env`; read keys with `grep -E '^KEY=' .env | cut -d= -f2-`.
- johnbot is user-scope: `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service`. NEVER start the system unit. The execution engine inherits johnbot's dotenv env, so a flag flip = `.env` edit + user-scope restart.
- Never delete master data. Never `git reset --hard`.
- Every flag introduced here defaults to the NEW behaviour being OFF unless the spec says otherwise: `OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM` (unset = no `√hold`; `1` = legacy), `OPENCLAW_TRADE_WEIGHT_FACTOR` (unset = factor 1.0; `1` = legacy), `OPENCLAW_BENCH_RELATIVE_SIZING` (unset = shadow only; `1` = apply).
- Python tests: `cd /root/openclaw && python3 -m pytest <file> -q`. JS tests: `cd /root/openclaw && node --test <file>`.
- Run pytest with `POSTGRES_URI` present in the environment only when the test needs the DB; every test below stubs its DB surface.

---

## File map

| Path | Responsibility | Tasks |
|---|---|---|
| `src/backtest/regime_qualification.py` | shared per-sleeve gate (no benchmark leg after T1) | 1 |
| `src/strategies/lifecycle.py` | state machine; `MIN_EXCESS_*` removed | 1 |
| `src/backtest/activation_assigner.py`, `src/backtest/eligibility_assigner.py` | per-regime eligibility, sliders only | 1 |
| `src/lib/promotion_service.js` | candidate→live gate, no benchmark leg | 2 |
| `src/execution/strategy_weights.py` | `_regime_weight` flag | 3 |
| `src/channels/api/server.js` | Eff.Sharpe = raw sleeve Sharpe; labels | 3, 4 |
| `src/execution/regime_blended_sizer.py` | trade-factor flag; acting-gate exemption; hurdle wiring; cap exemptions | 4, 8, 10, 11 |
| `src/execution/strategy_similarity.py` | trade-factor flag in gate replica; return-corr-only rule | 4, 12 |
| `src/strategies/base.py` | `benchmark_sleeve` class attr | 6 |
| `src/execution/benchmark_sleeve.py` (new) | who is a benchmark sleeve; benchmark tickers of a book | 6 |
| `src/strategies/implementations/S_beta_spy.py` (new), `src/strategies/registry.py` | the beta sleeve | 7 |
| `src/execution/benchmark_sizing.py` (new) | `apply_benchmark_hurdle`, `S_m` provider + cache, shadow line | 9 |
| `scripts/bench_relative_sizing_replay.py` (new) | read-only OFF-vs-ON book diff | 13 |
| `docs/archive/changelog.md`, `CLAUDE.md`, spec Status | docs | 17 |

---

### Task 1: Remove the R1 benchmark leg from the Python gates

**Files:**
- Modify: `src/backtest/regime_qualification.py` (module docstring lines 17–36; `class_thresholds`; delete `benchmark_leg_passes`, `log_bench_gate_skip`, `log_bench_gate_verdict`; `qualifies_regime`)
- Modify: `src/strategies/lifecycle.py:112-146` (constants + `min_excess_sharpe_vs_benchmark`) and the `can_transition` block at `:612-644`
- Modify: `src/backtest/activation_assigner.py` (imports `:75-77`, `_bench_log` `:162`, SELECT + loop `:296-345`)
- Modify: `src/backtest/eligibility_assigner.py` (imports `:49-50`, `_bench_log` `:68-74`, SELECT + loop `:95-140`)
- Delete: `tests/backtest/test_r1_assigners_benchmark_leg.py`
- Modify tests: `tests/backtest/test_benchmark_baseline.py` (drop everything from the `# ── qualifies_regime + the benchmark leg` banner at ~:147 to EOF, and the `qualifies_regime` import at :36), `tests/backtest/test_activation_assigner.py` (delete the six `test_*benchmark*` methods at :300-345), `tests/backtest/test_eligibility_assigner.py` (delete the six at :117-160), `tests/strategies/test_promotion_thresholds.py` (delete class `TestCandidateToLiveBenchmarkLeg` :50-119)
- Create: `tests/backtest/test_no_benchmark_gate.py`

**Interfaces:**
- Produces: `qualifies_regime(sharpe, trade_count, max_dd_pct, instrument_class='equity', calmar=None) -> bool` (no `benchmark_sharpe`/`sid`/`regime` kwargs). `class_thresholds()` no longer returns `min_excess_sharpe_vs_benchmark`. `strategies.lifecycle` no longer exports `MIN_EXCESS_SHARPE_VS_BENCHMARK*` / `min_excess_sharpe_vs_benchmark`.
- Keeps: `backtest/benchmark_baseline.py` untouched; `strategy_backtest_regimes.benchmark_sharpe` still written by `unified_backtest.py` (~:1439).

- [ ] **Step 1: Write the failing regression test**

```python
# tests/backtest/test_no_benchmark_gate.py
"""D1 (2026-08-29 spec): SPY regime Sharpe is a SIZING input only. No gate —
promotion, activation, eligibility — may read benchmark_sharpe."""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import regime_qualification as rq          # noqa: E402
from strategies import lifecycle as lc                     # noqa: E402


def test_qualifies_regime_has_no_benchmark_kwarg():
    params = inspect.signature(rq.qualifies_regime).parameters
    assert 'benchmark_sharpe' not in params
    assert set(params) == {'sharpe', 'trade_count', 'max_dd_pct', 'instrument_class', 'calmar'}


def test_sleeve_below_market_still_qualifies():
    # 1.2 < SPY LOW_VOL 2.03 — irrelevant to the gate now.
    assert rq.qualifies_regime(1.2, 150, 10.0, 'equity') is True


def test_class_thresholds_have_no_excess_key():
    assert 'min_excess_sharpe_vs_benchmark' not in rq.class_thresholds('equity')


def test_lifecycle_has_no_benchmark_constants():
    for name in ('MIN_EXCESS_SHARPE_VS_BENCHMARK', 'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS',
                 'min_excess_sharpe_vs_benchmark'):
        assert not hasattr(lc, name), name
    for name in ('benchmark_leg_passes', 'log_bench_gate_skip', 'log_bench_gate_verdict'):
        assert not hasattr(rq, name), name


def test_can_transition_ignores_benchmark_metadata(tmp_path):
    import json
    from strategies.lifecycle import LifecycleStateMachine, StrategyState
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'s1': {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': 'equity'}}}))
    sm = LifecycleStateMachine.from_manifest(p)
    ok, msg = sm.can_transition('s1', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.10, 'benchmark_sharpe': 9.0})
    assert ok is True, msg
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/backtest/test_no_benchmark_gate.py -q`
Expected: FAIL — `benchmark_sharpe` present in signature; `hasattr` assertions fail.

- [ ] **Step 3: Edit `regime_qualification.py`**

Replace the import block and `class_thresholds`:

```python
from strategies.lifecycle import _promotion_threshold  # noqa: E402

logger = logging.getLogger(__name__)


def class_thresholds(instrument_class: Optional[str]) -> dict:
    """Per-class gate thresholds in sleeve units: {min_sharpe, max_dd_pct,
    min_trades, min_calmar, dd_hard_cap_pct}. Unknown/None class falls back to
    equity (same as the JS getPromotionThreshold)."""
    thr = _promotion_threshold(instrument_class or 'equity')
    return {
        'min_sharpe': float(thr['min_sharpe']),
        'max_dd_pct': float(thr['max_drawdown']) * 100.0,
        'min_trades': int(thr['min_trades']),
        'min_calmar': float(thr.get('min_calmar', 0.5)),
        'dd_hard_cap_pct': float(thr.get('dd_hard_cap', 0.50)) * 100.0,
    }
```

Delete `benchmark_leg_passes`, `log_bench_gate_skip`, `log_bench_gate_verdict` entirely. Replace `qualifies_regime`:

```python
def qualifies_regime(sharpe, trade_count, max_dd_pct,
                     instrument_class: Optional[str] = 'equity',
                     calmar=None) -> bool:
    """True iff one regime sleeve clears all gates. Any missing metric fails
    closed (mirrors judgeRegimeSleeve's no_backtest behavior); a missing
    calmar only forfeits the DD escape hatch, not the whole sleeve.

    2026-08-29 (benchmark-relative sizing spec, D1): the R1 benchmark leg was
    REMOVED. SPY's regime Sharpe is a sizing input (execution.benchmark_sizing),
    never a gate."""
    thr = class_thresholds(instrument_class)
    if sharpe is None or trade_count is None or max_dd_pct is None:
        return False
    return (float(sharpe) > thr['min_sharpe']
            and dd_leg_passes(max_dd_pct, calmar, thr)
            and int(trade_count) >= thr['min_trades'])
```

In the module docstring, delete the R1/R1-assigners paragraphs (lines ~17–36) and add one line: `2026-08-29: benchmark leg removed (spec docs/specs/2026-08-29-benchmark-relative-sizing-spec.md D1).` Remove the now-unused `import math` if nothing else uses it.

- [ ] **Step 4: Edit `lifecycle.py`**

Delete lines 112–146 (the R1 comment block, `MIN_EXCESS_SHARPE_VS_BENCHMARK`, `_BY_CLASS`, `min_excess_sharpe_vs_benchmark`). In `can_transition`, delete the whole block from `# R1-assigners (2026-08-25): benchmark-relative leg` through the `return False, (f"candidate→live blocked: sharpe ... does not exceed benchmark_sharpe ...")` (lines ~612–644). The trades check above it stays the last guard in that branch.

- [ ] **Step 5: Edit `activation_assigner.py`**

Import block → `from backtest.regime_qualification import class_thresholds, dd_leg_passes  # noqa: E402`. Delete `_bench_log`. Replace the try/except SELECT (`:296-315`) with the plain query:

```python
        cur.execute("""
            SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
            FROM strategy_backtest_regimes
            WHERE run_id = %s
        """, (run_id,))
        rows = cur.fetchall()
```

In the loop delete the `bench = ...` line, the `benchmark_leg_passes` call, both log calls, and set `passes = legacy_pass`. Keep everything else byte-identical.

- [ ] **Step 6: Edit `eligibility_assigner.py`**

Same three edits: import only `class_thresholds, dd_leg_passes`; delete `_bench_log`; plain SELECT without `benchmark_sharpe` and without the try/except; loop `passes = legacy_pass`, no bench lines.

- [ ] **Step 7: Update tests**

`git rm tests/backtest/test_r1_assigners_benchmark_leg.py`. In `tests/backtest/test_benchmark_baseline.py` remove the `from backtest.regime_qualification import qualifies_regime` import and delete from the `# ── qualifies_regime + the benchmark leg` banner to end of file (the pure `regime_benchmark_sharpe` tests above it stay). Delete the six benchmark test methods in each assigner test file and the `TestCandidateToLiveBenchmarkLeg` class in `test_promotion_thresholds.py`. `tests/backtest/test_tail_stats_backtest_wiring.py:180` (`len(low_vol_row) == 15`) stays — the column is still written.

- [ ] **Step 8: Run the affected tests**

Run: `python3 -m pytest tests/backtest/test_no_benchmark_gate.py tests/backtest/test_benchmark_baseline.py tests/backtest/test_activation_assigner.py tests/backtest/test_eligibility_assigner.py tests/strategies/test_promotion_thresholds.py tests/backtest/test_tail_stats_backtest_wiring.py -q`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add src/backtest/regime_qualification.py src/strategies/lifecycle.py src/backtest/activation_assigner.py src/backtest/eligibility_assigner.py tests/backtest/test_no_benchmark_gate.py tests/backtest/test_benchmark_baseline.py tests/backtest/test_activation_assigner.py tests/backtest/test_eligibility_assigner.py tests/strategies/test_promotion_thresholds.py
git rm -q tests/backtest/test_r1_assigners_benchmark_leg.py
git commit -m "gates: remove the R1 SPY benchmark leg from qualification, lifecycle and both assigners (spec D1)"
git push origin main
```

---

### Task 2: Remove the R1 benchmark leg from `promotion_service.js`

**Files:**
- Modify: `src/lib/promotion_service.js` (`:24-30` comment, `:40-62` constants + `getMinExcessSharpeVsBenchmark`, `:71-77` ctx comment, `judgeRegimeSleeve` body `:98-~130`, `_regimeSleeves` SELECT `:191-193`, exports `:364-366`)
- Create: `tests/lib/promotion_no_benchmark_leg.test.js`

**Interfaces:**
- Produces: `judgeRegimeSleeve(row, thresholds, ctx = {})` returns only `'no_backtest' | 'sharpe' | 'max_dd' | 'trades'`. `module.exports` no longer has `getMinExcessSharpeVsBenchmark` / `MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS`.

- [ ] **Step 1: Write the failing test**

```js
// tests/lib/promotion_no_benchmark_leg.test.js
// D1 (2026-08-29): the candidate->live sleeve judge must not read benchmark_sharpe.
const test = require('node:test');
const assert = require('node:assert/strict');
const ps = require('../../src/lib/promotion_service');

test('a sleeve below the market still qualifies', () => {
  const thr = ps.getPromotionThreshold('equity');
  const fails = ps.judgeRegimeSleeve(
    { sharpe: 1.2, trade_count: 150, max_dd_pct: 5, calmar: 2, benchmark_sharpe: 2.03 },
    thr, { instrumentClass: 'equity', sid: 's', regime: 'LOW_VOL' });
  assert.deepEqual(fails, []);
});

test('benchmark exports are gone', () => {
  assert.equal(ps.getMinExcessSharpeVsBenchmark, undefined);
  assert.equal(ps.MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS, undefined);
});

test('legacy legs unchanged', () => {
  const thr = ps.getPromotionThreshold('equity');
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 0, trade_count: 150, max_dd_pct: 5 }, thr), ['sharpe']);
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 1, trade_count: 10, max_dd_pct: 5 }, thr), ['trades']);
  assert.deepEqual(ps.judgeRegimeSleeve(null, thr), ['no_backtest']);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/lib/promotion_no_benchmark_leg.test.js`
Expected: FAIL — first test gets `['benchmark_sharpe']`; exports still defined.

- [ ] **Step 3: Edit `promotion_service.js`**

Delete the R1 comment (`:24-30`), the `MIN_EXCESS_SHARPE_VS_BENCHMARK*` constants and `getMinExcessSharpeVsBenchmark` (`:40-62`). Replace the `ctx` comment above `judgeRegimeSleeve` with:

```js
// `ctx` is accepted for call-site compatibility (callers pass
// {instrumentClass, sid, regime}) and is unused since 2026-08-29 (spec D1:
// the R1 benchmark leg was removed; SPY's regime Sharpe sizes, it never gates).
```

Make the function body end right after the legacy legs:

```js
  if (!(s > thresholds.min_sharpe)) fails.push('sharpe');          // strict: must EXCEED
  if (!ddOk) fails.push('max_dd');
  if (n < thresholds.min_trades) fails.push('trades');
  return fails;
}
```

(delete `const legacyPass`, the whole benchmark block, `_msg`, and its log). Change the `_regimeSleeves` SELECT to `SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar FROM strategy_backtest_regimes WHERE run_id = $1`. Trim the export list to drop the two benchmark names.

- [ ] **Step 4: Run tests**

Run: `node --test tests/lib/promotion_no_benchmark_leg.test.js tests/lib/promotion_exit_hook_guard.test.js`
Expected: PASS (the exit-hook guard fixture's `benchmark_sharpe: null` is now simply ignored).

- [ ] **Step 5: Commit**

```bash
git add src/lib/promotion_service.js tests/lib/promotion_no_benchmark_leg.test.js
git commit -m "promotion: remove the R1 benchmark leg from judgeRegimeSleeve (spec D1)"
git push origin main
```

---

### Task 3: Cadence normalization out of strategy weights (+ dashboard Eff.Sharpe)

**Files:**
- Modify: `src/execution/strategy_weights.py` (module docstring `:7-10`, `_regime_weight` `:113-137`, `load_current` comment `:869-872`)
- Modify: `src/channels/api/server.js` (`_effSharpeOf` `:9356-9365`, header title `:9853`)
- Create: `tests/execution/test_strategy_weights_cadence_norm_off.py`

**Interfaces:**
- Produces: `strategy_weights.CADENCE_WEIGHT_NORM_ENV = 'OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM'`, `strategy_weights.cadence_weight_norm_enabled() -> bool`, `_regime_weight(effective_sharpe, holding_days) -> (weight, daily_weight)` with `daily_weight == weight` unless the env is `'1'`.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_strategy_weights_cadence_norm_off.py
"""D2 (2026-08-29 spec): daily_weight = effective_sharpe. The √hold divisor is
the REVERT path behind OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1."""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.strategy_weights as sw  # noqa: E402


def test_default_no_normalization(monkeypatch):
    monkeypatch.delenv(sw.CADENCE_WEIGHT_NORM_ENV, raising=False)
    w, dw = sw._regime_weight(2.0, 21.0)
    assert (w, dw) == (2.0, 2.0)


def test_revert_flag_restores_sqrt_hold(monkeypatch):
    monkeypatch.setenv(sw.CADENCE_WEIGHT_NORM_ENV, '1')
    w, dw = sw._regime_weight(2.0, 21.0)
    assert w == 2.0
    assert dw == 2.0 / math.sqrt(21.0)


def test_hold_floor_still_applies_under_revert(monkeypatch):
    monkeypatch.setenv(sw.CADENCE_WEIGHT_NORM_ENV, '1')
    assert sw._regime_weight(1.5, 0.2) == (1.5, 1.5)   # floored at 1 day
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_strategy_weights_cadence_norm_off.py -q`
Expected: FAIL — `CADENCE_WEIGHT_NORM_ENV` missing; default returns `2.0/√21`.

- [ ] **Step 3: Implement**

In `strategy_weights.py`, near the other module constants (after `OUE_TAU_DAYS`):

```python
# 2026-08-29 (spec docs/specs/2026-08-29-benchmark-relative-sizing-spec.md D2):
# cadence normalization RETIRED from sizing. daily_weight == effective_sharpe
# so weights (and the tangency S_adj built from them) are annualized-Sharpe
# units, directly comparable to the benchmark's regime Sharpe (S_m). Set the
# env to '1' to restore the legacy sharpe/√avg_holding_days divisor (revert
# path, same pattern as OPENCLAW_STRATEGY_CADENCE_STOP_NORM for brackets).
CADENCE_WEIGHT_NORM_ENV = 'OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM'


def cadence_weight_norm_enabled() -> bool:
    return os.environ.get(CADENCE_WEIGHT_NORM_ENV) == '1'
```

Replace `_regime_weight`:

```python
def _regime_weight(effective_sharpe: float, holding_days: float) -> tuple[float, float]:
    """Per-(strategy, regime) sizing weight from effective Sharpe.

    weight       = effective_sharpe                       (no OUE multiplier)
    daily_weight = effective_sharpe                       (since 2026-08-29, D2)
                 = effective_sharpe / sqrt(max(1, holding_days))
                                       only under OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1

    holding_days is still persisted as cadence_days (the sizer's signal window)
    by the caller — it just no longer divides the weight by default."""
    w = effective_sharpe
    if cadence_weight_norm_enabled():
        return w, w / math.sqrt(max(1, holding_days))
    return w, w
```

Update the module docstring lines 7–10 to `daily_weight = weight (cadence normalization retired 2026-08-29; √hold only under OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1)`. Confirm `import os` exists at the top of the module (add if missing).

In `server.js` replace `_effSharpeOf`:

```js
// Per-regime effective Sharpe — since 2026-08-29 (benchmark-relative sizing
// spec D2) this IS the raw sleeve Sharpe: strategy_weights.daily_weight ==
// effective_sharpe, no sqrt(avg holding days) divisor. Kept as a function so
// the column keeps its name and sort key.
function _effSharpeOf(b) {
  return (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
}
```

and the header title at `:9853` → `title="Sleeve Sharpe (cadence normalization retired 2026-08-29)"`. `DASHBOARD_BUILD` derives from the file mtime (`:33-36`), so stale tabs auto-reload; no manual bump.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_strategy_weights_cadence_norm_off.py tests/execution/test_strategy_weights_cadence_shape.py tests/execution/test_effective_sharpe_excludes_live.py -q && node --check src/channels/api/server.js`
Expected: PASS / no syntax error.

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_weights.py src/channels/api/server.js tests/execution/test_strategy_weights_cadence_norm_off.py
git commit -m "weights: retire sqrt(hold) cadence normalization (revert flag OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1); dashboard Eff.Sharpe = sleeve Sharpe (spec D2)"
git push origin main
```

---

### Task 4: Trade-count factor behind a flag (default OFF)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:1627-1633` (`_twf` block)
- Modify: `src/execution/strategy_similarity.py:693-710` (`_gate_weight_map`)
- Modify: `src/channels/api/server.js:7778` (label)
- Create: `tests/execution/test_trade_weight_factor_flag.py`

**Interfaces:**
- Produces: `regime_blended_sizer.TRADE_WEIGHT_FACTOR_ENV = 'OPENCLAW_TRADE_WEIGHT_FACTOR'`; `regime_blended_sizer.trade_weight_factor_enabled() -> bool`. Same helper re-exported in `strategy_similarity` via import.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_trade_weight_factor_flag.py
"""D3 (2026-08-29 spec): the √(ln n / ln anchor) trade factor is OFF by default.
Two single-strategy tickers with equal Sharpe but very different bt_n must be
sized equally unless OPENCLAW_TRADE_WEIGHT_FACTOR=1."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1,
            'min_signal_notional_pct': 0.00001, 'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG',
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff, bt_n):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff,
            'cadence_days': 5.0, 'bt_n': bt_n}


def _run(monkeypatch, rows, carried):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_BENCH_RELATIVE_SIZING'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def _targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders
            if o['action'] not in ('close_long', 'close_short')}


def test_factor_off_by_default_equal_targets(monkeypatch):
    monkeypatch.delenv(_sizer.TRADE_WEIGHT_FACTOR_ENV, raising=False)
    t = _targets(_run(monkeypatch, [_row('S_a', 2.0, 50), _row('S_b', 2.0, 5000)],
                      [_carried('S_a', 'ZZTA'), _carried('S_b', 'ZZTB')]))
    assert abs(t['ZZTA'] - t['ZZTB']) < 1e-6


def test_factor_on_downweights_thin_sleeve(monkeypatch):
    monkeypatch.setenv(_sizer.TRADE_WEIGHT_FACTOR_ENV, '1')
    t = _targets(_run(monkeypatch, [_row('S_a', 2.0, 50), _row('S_b', 2.0, 5000)],
                      [_carried('S_a', 'ZZTA'), _carried('S_b', 'ZZTB')]))
    assert t['ZZTA'] < t['ZZTB']
```

Note: this test stays valid after Task 8 wires the registry loader into the sizer — with no `POSTGRES_URI` in the test env the loader fails open to `set()` (logged), and with one present it performs a read-only registry read.

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_trade_weight_factor_flag.py -q`
Expected: FAIL — `TRADE_WEIGHT_FACTOR_ENV` missing (and, once defined, the first test fails until the sizer honours it).

- [ ] **Step 3: Implement**

In `regime_blended_sizer.py` next to `_ortho_enabled`:

```python
# 2026-08-29 (spec D3): the √(ln n / ln anchor) trade-count factor is OFF by
# default; set '1' to restore it (the anchor knob strategy_trade_factor_anchor
# only matters on that path).
TRADE_WEIGHT_FACTOR_ENV = 'OPENCLAW_TRADE_WEIGHT_FACTOR'


def trade_weight_factor_enabled() -> bool:
    return os.environ.get(TRADE_WEIGHT_FACTOR_ENV) == '1'
```

Replace the `_twf` block (`:1627-1633`):

```python
    _twf_on = trade_weight_factor_enabled()
    _twf = {sid: (_ogtf.trade_weight_factor(trade_count_by_strat.get(sid), anchor=_trade_factor_anchor)
                  if _twf_on else 1.0)
            for sid in set(weight_by_strat) | set(eff_weight_by_strat)}
    _cw_gate = {sid: w * _twf.get(sid, 1.0) for sid, w in weight_by_strat.items()}
    _cw_size = {sid: w * _twf.get(sid, 1.0) for sid, w in eff_weight_by_strat.items()}
    _n_tf = sum(1 for f in _twf.values() if abs(f - 1.0) > 1e-9)
    logger.info('trade_weight: %s; applied √(ln n) factor to %d/%d strategies (anchor n=%d)',
                'ON' if _twf_on else 'OFF (spec D3 2026-08-29)', _n_tf, len(_twf), _trade_factor_anchor)
```

In `strategy_similarity._gate_weight_map` add at the top: `from execution.regime_blended_sizer import trade_weight_factor_enabled` is a circular risk — instead duplicate the one-line check: `if os.environ.get('OPENCLAW_TRADE_WEIGHT_FACTOR') != '1': return {sid: r['daily_weight'] for sid, r in _current_weight_rows(regime_state).items()}` before the anchor lookup, and update the docstring to `Replicates the sizer's gate leg: daily_weight × trade_weight_factor(bt_n) when OPENCLAW_TRADE_WEIGHT_FACTOR=1, else daily_weight.`

`server.js:7778` label → `' · sizing contribution (conviction-weighted allocation: sleeve Sharpe × size_scalar × direction)'`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_trade_weight_factor_flag.py tests/execution/test_sizer_per_ticker_cap.py tests/execution/test_corr_cumsharpe_wiring.py -q`
Expected: PASS. If a test in the last two asserts a trade-factored value, set `OPENCLAW_TRADE_WEIGHT_FACTOR=1` in that test's env setup (it is testing the legacy path) rather than changing its expected numbers.

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py src/execution/strategy_similarity.py src/channels/api/server.js tests/execution/test_trade_weight_factor_flag.py
git commit -m "sizer: trade-count weight factor behind OPENCLAW_TRADE_WEIGHT_FACTOR (default OFF) (spec D3)"
git push origin main
```

---

### Task 5: Weights-rebuild shadow diff (ops, no code)

**Files:** none committed. Output goes to `/root/.learnings/` notes only if something is off.

- [ ] **Step 1: Print the would-be weight changes (read-only)**

```bash
cd /root/openclaw/src && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' ../.env | cut -d= -f2- | tr -d '"')" python3 - <<'EOF'
import sys; sys.path.insert(0, '.')
from execution.strategy_weights import _db
cur = _db().cursor()
cur.execute("""select regime_state, strategy_id, daily_weight::float, effective_sharpe::float, cadence_days::float
               from strategy_weights_by_regime where is_current order by 1, 4 desc""")
rows = cur.fetchall()
for R in ('LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'):
    rs = [r for r in rows if r[0] == R]
    old = sum(r[2] for r in rs); new = sum(r[3] for r in rs)
    print(f'{R}: n={len(rs)} Σdaily_weight old={old:.2f} new={new:.2f}')
    for _, sid, dw, eff, cad in rs[:8]:
        print(f'   {sid:45s} old={dw:.3f} new={eff:.3f} share_old={dw/old:.3f} share_new={eff/new:.3f} hold={cad:.1f}')
EOF
```

- [ ] **Step 2: Rebuild weights (writes `strategy_weights_by_regime`; run outside 13:00–20:15 UTC)**

```bash
cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" PYTHONPATH=src nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=cadence_norm_removed --verbose 2>&1 | tail -20
```

Expected: per-regime lines `Σ weight (=Σ effective_sharpe)`; row counts unchanged vs Step 1 (109 as of 2026-08-29); `daily_weight == effective_sharpe` on every current row:

```bash
cd /root/openclaw/src && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' ../.env | cut -d= -f2- | tr -d '"')" python3 -c "
import sys; sys.path.insert(0,'.')
from execution.strategy_weights import _db
c=_db().cursor(); c.execute('select count(*), count(*) filter (where abs(daily_weight-effective_sharpe)>1e-9) from strategy_weights_by_regime where is_current'); print(c.fetchone())"
```
Expected: `(N, 0)`.

- [ ] **Step 3: Note the per-ticker cap effect**

The next 15:00 ET sizer log line `corr_cumsharpe.live[...]` reports `cap_binds`; compare with the previous day's line (`journalctl --user -u johnbot.service --since yesterday | grep corr_cumsharpe.live`). If `cap_binds` drops to 0 and `gross_after_cap` jumps, record it in `/root/.learnings/LEARNINGS.md` as the input to the owed `PER_TICKER_CAP_SHARPE_FRAC` retune (spec §5). No code change in this task.

---

### Task 6: `benchmark_sleeve` flag — base class attribute + registry loader

**Files:**
- Modify: `src/strategies/base.py:136` (after `exit_hook`)
- Create: `src/execution/benchmark_sleeve.py`
- Create: `tests/execution/test_benchmark_sleeve.py`, `tests/strategies/test_benchmark_sleeve_attr.py`

**Interfaces:**
- Produces: `BaseStrategy.benchmark_sleeve: bool = False`; `benchmark_sleeve.PARAM_KEY = 'benchmark_sleeve'`; `benchmark_sleeve.load_benchmark_sleeve_ids(conn=None) -> set[str]`; `benchmark_sleeve.benchmark_tickers(ticker_meta: dict, bench_ids: set[str]) -> set[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_benchmark_sleeve_attr.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from strategies.base import BaseStrategy  # noqa: E402


def test_default_false():
    assert BaseStrategy.benchmark_sleeve is False


def test_subclass_can_opt_in():
    class _B(BaseStrategy):
        id = 'ZZT_bench'; name = 'x'
        benchmark_sleeve = True
        def generate_signals(self, prices, regime, universe, aux_data=None):
            return []
    assert _B.benchmark_sleeve is True
```

```python
# tests/execution/test_benchmark_sleeve.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import benchmark_sleeve as bs  # noqa: E402


class _Cur:
    def __init__(self, rows, fail=False): self.rows, self.fail = rows, fail
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if self.fail: raise RuntimeError('db down')
        assert "parameters ->> %s" in sql and params == (bs.PARAM_KEY,)
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows, fail=False): self._c = _Cur(rows, fail)
    def cursor(self): return self._c
    def close(self): pass


def test_loads_ids_from_registry_parameters():
    assert bs.load_benchmark_sleeve_ids(conn=_Conn([('S_beta_spy',)])) == {'S_beta_spy'}


def test_db_failure_is_empty_set_not_raise():
    assert bs.load_benchmark_sleeve_ids(conn=_Conn([], fail=True)) == set()


def test_benchmark_tickers_any_direction():
    meta = {'SPY': {'strategies': ['S_x', 'S_beta_spy'], 'directions': [1, 1]},
            'ZZTA': {'strategies': ['S_x'], 'directions': [1]},
            'QQQ': {'strategies': ['S_beta_spy'], 'directions': [-1]}}
    assert bs.benchmark_tickers(meta, {'S_beta_spy'}) == {'SPY', 'QQQ'}
    assert bs.benchmark_tickers(meta, set()) == set()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/strategies/test_benchmark_sleeve_attr.py tests/execution/test_benchmark_sleeve.py -q`
Expected: FAIL — attribute missing; module missing.

- [ ] **Step 3: Implement**

`base.py`, directly after the `exit_hook` attribute:

```python
    # Benchmark (beta) sleeve — spec docs/specs/2026-08-29-benchmark-relative-
    # sizing-spec.md §2.4. True marks a strategy whose conviction IS the
    # market's own regime Sharpe (e.g. S_beta_spy). The sizer reads the
    # mirrored strategy_registry.parameters.benchmark_sleeve to exempt the
    # sleeve's tickers from the acting-strategy gate, the S_adj − S_m hurdle
    # and both caps. Mirroring is done at registration (Task 15 runbook).
    benchmark_sleeve: bool = False
```

`src/execution/benchmark_sleeve.py`:

```python
"""benchmark_sleeve.py — which strategies are benchmark (beta) sleeves.

Spec: docs/specs/2026-08-29-benchmark-relative-sizing-spec.md §2.4.
Source of truth at runtime is strategy_registry.parameters ->> 'benchmark_sleeve'
(mirrored from the strategy class attribute BaseStrategy.benchmark_sleeve at
registration). The sizer never imports strategy classes, so the registry is
the only place it can read the flag from.

Fail-open contract: a DB failure returns an EMPTY set (logged). Consequence for
that cycle: no ticker is treated as a benchmark ticker, so the beta sleeve is
subject to the acting gate / hurdle / caps like any alpha ticker — a
conservative failure (less beta), never an unbounded one.
"""
from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

PARAM_KEY = 'benchmark_sleeve'


def load_benchmark_sleeve_ids(conn=None) -> set[str]:
    """Strategy ids whose registry parameters carry benchmark_sleeve=true."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM strategy_registry WHERE (parameters ->> %s) = 'true'",
                        (PARAM_KEY,))
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('[bench_sleeve] registry read failed (%s); no benchmark sleeves this cycle', e)
        return set()
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def benchmark_tickers(ticker_meta: dict, bench_ids: set[str]) -> set[str]:
    """Tickers with at least one benchmark-sleeve contributor, any direction.
    ticker_meta is the sizer's {ticker: {'strategies': [...], 'directions': [...]}}."""
    if not bench_ids:
        return set()
    return {t for t, m in ticker_meta.items()
            if any(s in bench_ids for s in (m or {}).get('strategies', []))}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/strategies/test_benchmark_sleeve_attr.py tests/execution/test_benchmark_sleeve.py tests/strategies/test_exit_hook_interface.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/base.py src/execution/benchmark_sleeve.py tests/strategies/test_benchmark_sleeve_attr.py tests/execution/test_benchmark_sleeve.py
git commit -m "benchmark sleeve: BaseStrategy.benchmark_sleeve attr + registry-backed loader (spec §2.4)"
git push origin main
```

---

### Task 7: `S_beta_spy` strategy

**Files:**
- Create: `src/strategies/implementations/S_beta_spy.py`
- Modify: `src/strategies/registry.py:17` (`_IMPL_MAP` entry)
- Create: `tests/strategies/test_beta_spy.py`

**Interfaces:**
- Produces: class `BetaSpy(BaseStrategy)` with `id='S_beta_spy'`, `benchmark_sleeve=True`, `signal_frequency='daily'`, `active_in_regimes` = all four; `generate_signals(prices, regime, universe, aux_data=None)` → exactly one `Signal('SPY','LONG', …, signal_params={'hold_days': 21, 'benchmark_sleeve': True, 'regime': <state>})` when SPY has a positive last close, else `[]`. Module constants `STRATEGY_ID='S_beta_spy'`, `INSTRUMENT_CLASS='etp'`, `BENCHMARK='SPY'`, `HOLD_DAYS=21`.
- Consumes: `resolve_hold_cap` reads `signal_params['hold_days']` (`src/backtest/open_book.py:49`); live hold cap = `configured_max_hold_days` default 21.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_beta_spy.py
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.implementations.S_beta_spy import BetaSpy, STRATEGY_ID, HOLD_DAYS  # noqa: E402


def _panel(n=30, spy=True):
    idx = pd.bdate_range('2026-01-02', periods=n)
    cols = {'ZZTA': np.linspace(50, 60, n)}
    if spy:
        cols['SPY'] = np.linspace(500, 520, n)
    return pd.DataFrame(cols, index=idx)


def test_contract():
    assert BetaSpy.id == STRATEGY_ID == 'S_beta_spy'
    assert BetaSpy.benchmark_sleeve is True
    assert BetaSpy.signal_frequency == 'daily'
    assert set(BetaSpy.active_in_regimes) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}


def test_one_long_spy_per_bar_every_regime():
    s = BetaSpy()
    for R in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'):
        out = s.generate_signals(_panel(), {'state': R}, ['SPY', 'ZZTA'])
        assert len(out) == 1
        sig = out[0]
        assert (sig.ticker, sig.direction) == ('SPY', 'LONG')
        assert sig.entry_price == 520.0
        assert sig.signal_params['hold_days'] == HOLD_DAYS == 21
        assert sig.signal_params['benchmark_sleeve'] is True
        # levels never bind: stop far below, targets far above
        assert sig.stop_loss < 0.7 * sig.entry_price
        assert sig.target_1 > 3.0 * sig.entry_price


def test_no_spy_column_or_bad_price_is_silent():
    s = BetaSpy()
    assert s.generate_signals(_panel(spy=False), {'state': 'LOW_VOL'}, ['ZZTA']) == []
    p = _panel(); p.loc[p.index[-1], 'SPY'] = np.nan
    assert s.generate_signals(p, {'state': 'LOW_VOL'}, ['SPY']) == []
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['SPY']) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/strategies/test_beta_spy.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/strategies/implementations/S_beta_spy.py
"""
Beta sleeve — long SPY, always.

Spec: docs/specs/2026-08-29-benchmark-relative-sizing-spec.md §2.4 (D4, D8).

This strategy carries no alpha. It exists so that the market's own
regime-conditioned Sharpe enters the sizer through the normal rails: its
backtest sleeves ARE (up to honest costs and entry-regime tagging) SPY's
regime Sharpes, the activation slider makes it dormant in regimes where SPY
does not clear the slider (TRANSITIONING / HIGH_VOL / CRISIS at the 1.0 slider
as of 2026-08-29), and in LOW_VOL its conviction becomes the base that alpha
tickers must beat (execution.benchmark_sizing.apply_benchmark_hurdle).

Signal shape: ONE LONG SPY per bar, hold_days = 21 (= the live default hold
cap, so the exit-hook hold-cap parity guard is satisfied). In the backtest the
daily emissions become overlapping 21-day lots whose equal-weighted daily
return is exactly SPY's daily return; live, the sizer's rebalance step nets the
carried target against the held position, so there is no churn. Stop/targets
are set so they never bind — the sleeve carries no bracket edge to protect.

benchmark_sleeve = True must be mirrored into strategy_registry.parameters at
registration (the sizer reads the registry, never this class).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['BetaSpy']

STRATEGY_ID      = 'S_beta_spy'
INSTRUMENT_CLASS = 'etp'
BENCHMARK        = 'SPY'
HOLD_DAYS        = 21
STOP_FRAC        = 0.60    # stop 40% below entry: never binds inside a 21-day lot
TARGET_FRACS     = (5.0, 6.0, 7.0)


class BetaSpy(BaseStrategy):
    id                = STRATEGY_ID
    name              = 'Beta sleeve — long SPY'
    description       = 'Benchmark sleeve: long SPY every bar, hold 21; sized on the market regime Sharpe.'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1
    benchmark_sleeve  = True

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or BENCHMARK not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return []
        px = prices[BENCHMARK].iloc[-1]
        try:
            px = float(px)
        except (TypeError, ValueError):
            return []
        if not (px == px and px > 0):
            print('[debug] signals=0 (SPY last close missing)', file=sys.stderr)
            return []
        state = regime.get('state') if isinstance(regime, dict) else None
        return [Signal(
            ticker            = BENCHMARK,
            direction         = 'LONG',
            entry_price       = px,
            stop_loss         = round(px * STOP_FRAC, 4),
            target_1          = round(px * TARGET_FRACS[0], 4),
            target_2          = round(px * TARGET_FRACS[1], 4),
            target_3          = round(px * TARGET_FRACS[2], 4),
            position_size_pct = 0.10,
            confidence        = 'HIGH',
            signal_params     = {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': state},
        )]
```

`registry.py` `_IMPL_MAP`: add `'S_beta_spy': ('strategies.implementations.S_beta_spy', 'BetaSpy'),` (alphabetical position is irrelevant; put it after the last entry).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/strategies/test_beta_spy.py -q && python3 -c "import sys; sys.path.insert(0,'src'); from strategies.registry import _IMPL_MAP; print(_IMPL_MAP['S_beta_spy'])"`
Expected: PASS; prints the tuple.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/S_beta_spy.py src/strategies/registry.py tests/strategies/test_beta_spy.py
git commit -m "strategies: S_beta_spy beta sleeve (long SPY every bar, hold 21, benchmark_sleeve=True) (spec §2.4)"
git push origin main
```

(Do NOT touch `manifest.json` / `strategy_signatures*.json`; the signature sidecar regenerates on its own timer and its diff stays unstaged per Global Constraints.)

---

### Task 8: Acting-strategy gate exemption for benchmark tickers

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (load benchmark ids right after `ticker_w = defaultdict(float, _size_adj)` at `:1680`; gate loop `:1689-1691`)
- Create: `tests/execution/test_sizer_benchmark_acting_gate.py`

**Interfaces:**
- Consumes: `execution.benchmark_sleeve.load_benchmark_sleeve_ids`, `benchmark_tickers` (Task 6).
- Produces (locals used by Tasks 10–11): `_bench_ids: set[str]`, `_bench_tkrs: set[str]` computed once per `_sharpe_cadence_path` call, immediately after `ticker_w = defaultdict(float, _size_adj)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_sizer_benchmark_acting_gate.py
"""Spec §2.4 exemption (i): a ticker with a benchmark-sleeve contributor passes
min_acting_strategies even when it is the only strategy acting on it."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params(min_acting=2):
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0,
            'min_acting_strategies': min_acting}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


def run(monkeypatch, rows, carried, bench_ids, params=None):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE',
              'OPENCLAW_BENCH_RELATIVE_SIZING', 'OPENCLAW_TRADE_WEIGHT_FACTOR'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set(bench_ids)):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=params or _params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_lone_benchmark_ticker_passes_min_acting_2(monkeypatch):
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0)], [_carried('S_beta_spy', 'SPY')], {'S_beta_spy'}))
    assert 'SPY' in t and t['SPY'] > 0


def test_lone_alpha_ticker_still_gated(monkeypatch):
    t = targets(run(monkeypatch, [_row('S_x', 2.0)], [_carried('S_x', 'ZZTA')], {'S_beta_spy'}))
    assert 'ZZTA' not in t


def test_registry_flag_is_the_switch(monkeypatch):
    # Same book, but the registry says nobody is a benchmark sleeve -> SPY gated like any ticker.
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0)], [_carried('S_beta_spy', 'SPY')], set()))
    assert 'SPY' not in t
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_acting_gate.py -q`
Expected: first test FAILS (SPY gated out).

- [ ] **Step 3: Implement**

In `regime_blended_sizer.py`, right after `ticker_w = defaultdict(float, _size_adj)` and its log line (`:1680-1683`):

```python
    # Benchmark (beta) sleeve tickers — spec 2026-08-29 §2.4. Read once per
    # cycle from the registry; fail-open to "no benchmark tickers" (logged in
    # the loader). Used by the acting gate below, the S_adj − S_m hurdle and
    # both caps further down.
    from execution import benchmark_sleeve as _bsl
    _bench_ids = _bsl.load_benchmark_sleeve_ids()
    _bench_tkrs = _bsl.benchmark_tickers(ticker_meta, _bench_ids)
    if _bench_tkrs:
        logger.info('bench_sleeve: %d benchmark ticker(s) %s from sleeves %s',
                    len(_bench_tkrs), sorted(_bench_tkrs), sorted(_bench_ids))
```

Gate loop:

```python
        gated_out = [tkr for tkr in list(ticker_w.keys())
                     if acting_n.get(tkr, 0) < min_acting and tkr not in _bench_tkrs]
```

and extend the comment above the block: `Benchmark tickers are exempt (their conviction is the market's own; spec §2.4 (i)).`

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_acting_gate.py tests/execution/test_sizer_per_ticker_cap.py tests/execution/test_trade_weight_factor_flag.py -q`
Expected: PASS. (`test_sizer_per_ticker_cap.py` does not stub the loader; with no `POSTGRES_URI` the loader fails open to `set()` and logs a warning — acceptable. If `POSTGRES_URI` is in the shell env, the loader hits the real registry read-only; also acceptable.)

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_benchmark_acting_gate.py
git commit -m "sizer: benchmark-sleeve tickers exempt from min_acting_strategies (spec §2.4 i)"
git push origin main
```

---

### Task 9: `execution/benchmark_sizing.py` — hurdle, `S_m` provider, shadow line

**Files:**
- Create: `src/execution/benchmark_sizing.py`
- Create: `tests/execution/test_benchmark_sizing.py`

**Interfaces:**
- Produces:
  - `BENCH_SIZING_ENV = 'OPENCLAW_BENCH_RELATIVE_SIZING'`; `bench_relative_sizing_enabled() -> bool`
  - `CONFIG_KEY = 'benchmark_regime_sharpe'`
  - `apply_benchmark_hurdle(ticker_w: dict[str, float], s_m: float | None, bench_tickers: set[str]) -> tuple[dict[str, float], list[str]]` — pure; `(hurdled_weights, dropped_tickers)`
  - `regime_benchmark_sharpe_for_sizing(regime_state: str, run_date, *, benchmark='SPY', conn=None, compute=None) -> float | None` — reads/refreshes the `pipeline_config[CONFIG_KEY]` cache `{"as_of": "YYYY-MM-DD", "benchmark": "SPY", "start": "2016-04-11", "by_regime": {...}}`; `compute` defaults to `backtest.benchmark_baseline.regime_benchmark_sharpe`
  - `shadow_line(regime_state, s_m, before, after, dropped, bench_tickers, lam_nav, *, mode='shadow') -> str`
- Consumes: `backtest.unified_backtest.DEFAULT_START_DATE` (`'2016-04-11'`), `backtest.benchmark_baseline.regime_benchmark_sharpe(start, end, benchmark=, min_obs=)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_benchmark_sizing.py
"""Spec §2.5: per-ticker hurdle S_adj − S_m, benchmark ticker exempt, shorts
symmetric (D7), S_m=None passthrough; S_m provider cache; shadow line."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import benchmark_sizing as bz  # noqa: E402


# ── apply_benchmark_hurdle ───────────────────────────────────────────────────
def test_hurdle_subtracts_and_drops():
    import pytest
    w = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5, 'ZZTC': -2.5, 'ZZTD': -1.0}
    out, dropped = bz.apply_benchmark_hurdle(w, 2.0, {'SPY'})
    assert set(out) == {'SPY', 'ZZTA', 'ZZTC'}
    assert out['SPY'] == 2.0                       # benchmark ticker: no subtraction
    assert out['ZZTA'] == pytest.approx(0.6)       # 2.6 − 2.0
    assert out['ZZTC'] == pytest.approx(-0.5)      # short: |−2.5| − 2.0, sign kept (D7)
    assert sorted(dropped) == ['ZZTB', 'ZZTD']


def test_exact_tie_is_dropped():
    out, dropped = bz.apply_benchmark_hurdle({'ZZTA': 2.0}, 2.0, set())
    assert out == {} and dropped == ['ZZTA']


def test_none_s_m_is_passthrough():
    w = {'ZZTA': 0.3, 'SPY': 2.0}
    out, dropped = bz.apply_benchmark_hurdle(w, None, {'SPY'})
    assert out == w and dropped == [] and out is not w


def test_input_not_mutated():
    w = {'ZZTA': 2.6}
    bz.apply_benchmark_hurdle(w, 2.0, set())
    assert w == {'ZZTA': 2.6}


# ── regime_benchmark_sharpe_for_sizing ───────────────────────────────────────
class _Cur:
    def __init__(self, store): self.store = store; self.last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.last = (sql, params)
        if sql.lstrip().upper().startswith('SELECT'):
            self._row = (self.store.get(params[0]),) if self.store.get(params[0]) is not None else None
        else:
            key, value = params[0], params[1]
            self.store[key] = value
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, store): self.store = store; self.c = _Cur(store); self.commits = 0
    def cursor(self): return self.c
    def commit(self): self.commits += 1
    def close(self): pass


def test_computes_persists_and_reuses_cache():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append((start, end, benchmark))
        return {'LOW_VOL': 2.03, 'TRANSITIONING': 0.55, 'HIGH_VOL': 0.6, 'CRISIS': None}
    store = {}
    conn = _Conn(store)
    v = bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn, compute=compute)
    assert v == 2.03 and calls == [('2016-04-11', '2026-08-29', 'SPY')]
    payload = json.loads(store[bz.CONFIG_KEY])
    assert payload['as_of'] == '2026-08-29' and payload['by_regime']['CRISIS'] is None
    # second call same day: cache hit, no recompute
    v2 = bz.regime_benchmark_sharpe_for_sizing('HIGH_VOL', date(2026, 8, 29), conn=conn, compute=compute)
    assert v2 == 0.6 and len(calls) == 1
    # a new day recomputes
    bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 30), conn=conn, compute=compute)
    assert len(calls) == 2


def test_thin_regime_and_failures_return_none():
    conn = _Conn({})
    assert bz.regime_benchmark_sharpe_for_sizing('CRISIS', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: {'CRISIS': None}) is None
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: {}) is None
    def boom(*a, **k): raise RuntimeError('parquet gone')
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn, compute=boom) is None


# ── shadow_line ──────────────────────────────────────────────────────────────
def test_shadow_line_reports_shares_and_moves():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, lam_nav=100_000.0)
    assert line.startswith("bench_sizing.shadow[LOW_VOL]: S_m=2.00 bench=['SPY'] dropped=1/3")
    assert 'beta_share_before=0.328 beta_share_after=0.769' in line
    assert 'ZZTB' in line and 'top_moves=' in line
    assert bz.shadow_line('LOW_VOL', None, before, before, [], {'SPY'}, 100_000.0).startswith(
        'bench_sizing.shadow[LOW_VOL]: S_m=None')
    assert bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, mode='apply').startswith(
        'bench_sizing.apply[')
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# src/execution/benchmark_sizing.py
"""benchmark_sizing.py — benchmark-relative sizing rule C (spec §2.5).

Per ticker, after the sizer has rebuilt its sizing basis from the tangency
S_adj (regime_blended_sizer._sharpe_cadence_path, `ticker_w = defaultdict(float,
_size_adj)`):

    benchmark ticker :  w = S_adj                       (beta base, exempt)
    alpha ticker     :  ex = |S_adj| − S_m
                        w = sign(S_adj) · ex   if ex > 0
                        dropped                otherwise (ties drop: "not above the market")

S_m is the benchmark's (SPY) annualized Sharpe over the days tagged with the
sizer's regime-of-record, computed by backtest.benchmark_baseline over the
canonical fleet window (unified_backtest.DEFAULT_START_DATE .. run_date) so it
is like-for-like with the sleeve Sharpes S_adj is built from (D2 removed the
√hold divisor precisely so these are the same units). Cached per day in
pipeline_config['benchmark_regime_sharpe'] for the dashboard and the intraday
lane. Any failure -> None -> the sizer sizes on raw S_adj (fail-open, logged).

Flag: OPENCLAW_BENCH_RELATIVE_SIZING. Unset/0 = SHADOW (the sizer logs what the
rule would do every cycle, changes nothing). '1' = APPLY.
"""
from __future__ import annotations
import json
import logging
import math
import os

logger = logging.getLogger(__name__)

BENCH_SIZING_ENV = 'OPENCLAW_BENCH_RELATIVE_SIZING'
CONFIG_KEY = 'benchmark_regime_sharpe'
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def bench_relative_sizing_enabled() -> bool:
    return os.environ.get(BENCH_SIZING_ENV) == '1'


def apply_benchmark_hurdle(ticker_w: dict, s_m, bench_tickers: set) -> tuple[dict, list]:
    """Pure. Returns (hurdled_weights, dropped_tickers). Never mutates ticker_w.
    s_m None -> copy of the input, nothing dropped (fail-open)."""
    if s_m is None:
        return dict(ticker_w), []
    s_m = float(s_m)
    out: dict = {}
    dropped: list = []
    for tkr, s in ticker_w.items():
        s = float(s)
        if tkr in bench_tickers:
            out[tkr] = s
            continue
        ex = abs(s) - s_m
        if ex > 0.0:
            out[tkr] = math.copysign(ex, s)
        else:
            dropped.append(tkr)
    return out, dropped


def _read_cache(cur) -> dict | None:
    cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (CONFIG_KEY,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return None


def _write_cache(conn, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (CONFIG_KEY, json.dumps(payload, sort_keys=True),
             'Benchmark (SPY) regime Sharpe used by the sizer hurdle S_adj − S_m '
             '(spec 2026-08-29 §2.5.1). Refreshed once per run_date by the sizer; '
             'window = unified_backtest.DEFAULT_START_DATE .. as_of.'))
    conn.commit()


def regime_benchmark_sharpe_for_sizing(regime_state: str, run_date, *, benchmark: str = 'SPY',
                                       conn=None, compute=None):
    """S_m for regime_state as of run_date, or None. Reuses the pipeline_config
    cache when its as_of == run_date (the 5-minute intraday lane must not
    re-read the parquet); otherwise computes all four regimes, persists, returns."""
    as_of = run_date.strftime('%Y-%m-%d') if hasattr(run_date, 'strftime') else str(run_date)[:10]
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cached = _read_cache(cur)
        if cached and cached.get('as_of') == as_of and cached.get('benchmark') == benchmark:
            by_regime = cached.get('by_regime') or {}
        else:
            from backtest.unified_backtest import DEFAULT_START_DATE
            if compute is None:
                from backtest.benchmark_baseline import regime_benchmark_sharpe as compute
            by_regime = compute(DEFAULT_START_DATE, as_of, benchmark=benchmark) or {}
            by_regime = {r: (float(by_regime[r]) if by_regime.get(r) is not None else None)
                         for r in CANONICAL_REGIMES}
            if any(v is not None for v in by_regime.values()):
                _write_cache(conn, {'as_of': as_of, 'benchmark': benchmark,
                                    'start': DEFAULT_START_DATE, 'by_regime': by_regime})
            else:
                logger.warning('[bench_sizing] S_m compute returned no regimes for %s..%s', DEFAULT_START_DATE, as_of)
                return None
        v = by_regime.get(regime_state)
        if v is None or not math.isfinite(float(v)):
            return None
        return float(v)
    except Exception as e:
        logger.warning('[bench_sizing] S_m unavailable (%s: %s); sizing on raw S_adj', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _shares(w: dict, bench: set) -> tuple[float, float]:
    gross = sum(abs(v) for v in w.values())
    if gross <= 0:
        return 0.0, 0.0
    return gross, sum(abs(v) for t, v in w.items() if t in bench) / gross


def shadow_line(regime_state: str, s_m, before: dict, after: dict, dropped: list,
                bench_tickers: set, lam_nav: float, *, mode: str = 'shadow') -> str:
    """One line per cycle. Dollar moves are computed by normalizing BOTH books
    to lam_nav (the sizer's Σ|target| = λ·NAV rule) so the diff is in the units
    the book will actually move."""
    g0, beta0 = _shares(before, bench_tickers)
    g1, beta1 = _shares(after, bench_tickers)
    usd0 = {t: (v / g0) * lam_nav for t, v in before.items()} if g0 > 0 else {}
    usd1 = {t: (v / g1) * lam_nav for t, v in after.items()} if g1 > 0 else {}
    moves = sorted(((t, round(usd1.get(t, 0.0) - usd0.get(t, 0.0), 2)) for t in set(usd0) | set(usd1)),
                   key=lambda kv: -abs(kv[1]))
    moved = sum(abs(m) for _, m in moves) / (2.0 * lam_nav) if lam_nav > 0 else 0.0
    s_m_txt = 'None' if s_m is None else f'{float(s_m):.2f}'
    return (f'bench_sizing.{mode}[{regime_state}]: S_m={s_m_txt} bench={sorted(bench_tickers)} '
            f'dropped={len(dropped)}/{len(before)} beta_share_before={beta0:.3f} beta_share_after={beta1:.3f} '
            f'gross_moved_frac={moved:.3f} dropped_tickers={sorted(dropped)[:15]} top_moves={moves[:10]}')
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q`
Expected: PASS. (If `test_hurdle_subtracts_and_drops` fails on the float repr of `0.6000000000000001`, compare with `pytest.approx` instead — the arithmetic is the contract, not the repr.)

- [ ] **Step 5: Commit**

```bash
git add src/execution/benchmark_sizing.py tests/execution/test_benchmark_sizing.py
git commit -m "sizing: benchmark_sizing — S_adj − S_m hurdle, S_m provider with pipeline_config cache, shadow line (spec §2.5)"
git push origin main
```

---

### Task 10: Wire rule C into the sizer (shadow by default)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (immediately after the Task 8 benchmark-ticker block, before the acting gate)
- Create: `tests/execution/test_sizer_benchmark_hurdle_wiring.py`

**Interfaces:**
- Consumes: Task 8 locals `_bench_tkrs`; Task 9 `benchmark_sizing.*`; existing `_post_corr_cumsharpe_log(line)`, `lam`, `nav`, `regime_state`.
- Produces: log line `bench_sizing.shadow[REGIME]: …` every cycle (posted to #botjohn-log while `pipeline_config.corr_cumsharpe_log_until` is in the future, daily lane only); with the flag ON, `bench_sizing.apply[REGIME]: …` and the hurdled `ticker_w`/`ticker_meta`.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_sizer_benchmark_hurdle_wiring.py
"""Spec §2.5 wiring: OFF -> byte-identical sizing + shadow line; ON -> hurdle
applied, benchmark ticker exempt and uncapped by the hurdle."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


ROWS = [_row('S_beta_spy', 2.0), _row('S_hi', 2.6), _row('S_lo', 1.5)]
CARRIED = [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA'), _carried('S_lo', 'ZZTB')]


def run(monkeypatch, flag, s_m=2.0, lines=None):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY'):
        monkeypatch.delenv(g, raising=False)
    if flag: monkeypatch.setenv(bz.BENCH_SIZING_ENV, '1')
    else:    monkeypatch.delenv(bz.BENCH_SIZING_ENV, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(CARRIED))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: (lines.append(line) if lines is not None else None))
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    # The per-ticker conviction cap is armed under EOD_RECONCILE and would clamp
    # SPY/ZZTA here; it is not this task's subject (Task 11 exempts benchmark
    # tickers), so lift it out of the way.
    monkeypatch.setattr(_sizer, 'PER_TICKER_CAP_SHARPE_FRAC', 10.0)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(ROWS)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value={'S_beta_spy'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=s_m):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_off_is_byte_identical_and_logs_shadow(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=False, lines=lines))
    gross = LAM * NAV
    # raw S_adj shares: 2.0 : 2.6 : 1.5 (per-ticker cap lifted in run())
    assert abs(t['SPY'] - gross * 2.0 / 6.1) < 1e-6
    assert abs(t['ZZTA'] - gross * 2.6 / 6.1) < 1e-6
    assert abs(t['ZZTB'] - gross * 1.5 / 6.1) < 1e-6
    assert any(l.startswith('bench_sizing.shadow[LOW_VOL]: S_m=2.00') and 'dropped=1/3' in l for l in lines)


def test_on_applies_hurdle(monkeypatch):
    t = targets(run(monkeypatch, flag=True))
    # after hurdle: SPY 2.0 (exempt), ZZTA 0.6, ZZTB dropped -> gross 2.6
    gross = LAM * NAV
    assert 'ZZTB' not in t
    assert abs(t['ZZTA'] - gross * 0.6 / 2.6) < 1e-6
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6     # benchmark ticker keeps its full S_adj


def test_on_with_no_s_m_falls_back_to_raw(monkeypatch):
    t = targets(run(monkeypatch, flag=True, s_m=None))
    assert set(t) == {'SPY', 'ZZTA', 'ZZTB'}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_hurdle_wiring.py -q`
Expected: FAIL — no shadow line; ON path unchanged.

- [ ] **Step 3: Implement**

In `regime_blended_sizer.py`, directly after the Task 8 block (still before `min_acting = …` is used by the gate; note `min_acting`/`acting_n` were computed earlier at `:1649-1650` — keep them):

```python
    # Rule C — benchmark-relative sizing (spec 2026-08-29 §2.5). Alpha tickers
    # are sized on |S_adj| − S_m (sign preserved), benchmark tickers keep S_adj.
    # SHADOW unless OPENCLAW_BENCH_RELATIVE_SIZING=1. Whole block fail-open.
    try:
        from execution import benchmark_sizing as _bsz
        _s_m = _bsz.regime_benchmark_sharpe_for_sizing(regime_state, date.today())
        _hurdled, _bench_dropped = _bsz.apply_benchmark_hurdle(dict(ticker_w), _s_m, _bench_tkrs)
        _bench_on = _bsz.bench_relative_sizing_enabled()
        _bline = _bsz.shadow_line(regime_state, _s_m, dict(ticker_w), _hurdled, _bench_dropped,
                                  _bench_tkrs, lam * nav, mode='apply' if _bench_on else 'shadow')
        logger.info(_bline)
        if os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') != '1':
            _post_corr_cumsharpe_log(_bline)
        if _bench_on:
            for _t in _bench_dropped:
                ticker_meta.pop(_t, None)
            ticker_w = defaultdict(float, _hurdled)
    except Exception as e:
        logger.warning('bench_sizing: failed (%s: %s); sizing on raw S_adj', type(e).__name__, e)
```

`date` is already imported at module top (`from datetime import date`). The existing `_post_corr_cumsharpe_log` gate (`pipeline_config.corr_cumsharpe_log_until`) governs whether the line reaches Discord; the log line is always written.

Also in this step: the sizer now calls `regime_benchmark_sharpe_for_sizing` on every cycle, which WRITES the `pipeline_config` cache when a DB is reachable. Tests written in Tasks 4 and 8 drive `size_positions` without stubbing it; add
`_mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=None)` to the `with` block of `_run()` in `tests/execution/test_trade_weight_factor_flag.py` and of `run()` in `tests/execution/test_sizer_benchmark_acting_gate.py`, so no test ever touches that row. (`test_sizer_per_ticker_cap.py` runs without `POSTGRES_URI` in its fixtures; with none in the shell env the provider fails open to `None` and writes nothing.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_sizer_benchmark_acting_gate.py tests/execution/test_sizer_per_ticker_cap.py tests/execution/test_trade_weight_factor_flag.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_benchmark_hurdle_wiring.py
git commit -m "sizer: wire S_adj − S_m hurdle (OPENCLAW_BENCH_RELATIVE_SIZING; shadow line every cycle) (spec §2.5)"
git push origin main
```

---

### Task 11: Benchmark tickers exempt from the per-ticker cap and excluded from cluster capping

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (per-ticker cap loop `:~1760-1770`; `_apply_asset_corr_cap` signature + body `:2623-2655`; its call site `:~1809`)
- Modify: `tests/execution/test_sizer_per_ticker_cap.py:232` (spy signature gains `**kw`)
- Create: `tests/execution/test_sizer_benchmark_cap_exemptions.py`

**Interfaces:**
- Produces: `_apply_asset_corr_cap(target_usd, conviction, nav, lam=1.0, exclude=None)`; benchmark tickers skipped in the per-ticker cap loop.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_sizer_benchmark_cap_exemptions.py
"""Spec D6 / §2.6: no beta cap. Benchmark tickers skip the per-ticker
conviction cap and never enter the asset-correlation cluster filter."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM, CAP = 100_000.0, 2.0, _sizer.PER_TICKER_CAP_SHARPE_FRAC


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG', 'signal_date': date(2026, 8, 28),
            'entry_price': 100.0, 'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


def run(monkeypatch, rows, carried, corr_spy=None):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv(bz.BENCH_SIZING_ENV, '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    if corr_spy is not None:
        monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', corr_spy)
    else:
        monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value={'S_beta_spy'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=2.0):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_benchmark_ticker_not_capped_alpha_still_capped(monkeypatch):
    # SPY 2.0 (exempt) + ZZTA 2.6 -> hurdled 0.6. gross 2.6 -> SPY raw share 153.8k.
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0), _row('S_hi', 2.6)],
                    [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA')]))
    assert abs(t['SPY'] - LAM * NAV * 2.0 / 2.6) < 1e-6          # uncapped (cap would be 60k)
    assert t['ZZTA'] <= CAP * (2.6 + 1.0) * LAM * NAV + 1e-6    # alpha cap formula still reads raw S_adj


def test_cluster_cap_receives_exclude_set(monkeypatch):
    seen = {}
    def spy(target_usd, conviction, nav, lam=1.0, exclude=None):
        seen['exclude'] = set(exclude or ()); return target_usd
    run(monkeypatch, [_row('S_beta_spy', 2.0), _row('S_hi', 2.6)],
        [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA')], corr_spy=spy)
    assert seen['exclude'] == {'SPY'}


def test_apply_asset_corr_cap_reinserts_excluded_untouched(monkeypatch):
    monkeypatch.setattr(_sizer, '_load_asset_corr_cfg', lambda: (True, 0.6, 0.20))
    import execution.asset_correlation as _ac
    import execution.asset_correlation_filter as _acf
    monkeypatch.setattr(_ac, 'price_return_corr', lambda tickers, window=63: {t: {u: 0.9 for u in tickers} for t in tickers})
    calls = {}
    def cap(target_usd, conviction, corr, nav, cap_pct, corr_thr, lam):
        calls['tickers'] = set(target_usd)
        return {t: v * 0.5 for t, v in target_usd.items()}, {'clusters': [], 'total_gross_before': 0,
                                                             'total_gross_after': 0, 'released_usd': 0}
    monkeypatch.setattr(_acf, 'cap_correlated_clusters', cap)
    out = _sizer._apply_asset_corr_cap({'SPY': 150_000.0, 'ZZTA': 40_000.0, 'ZZTB': 30_000.0},
                                       {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 2.2}, NAV, lam=LAM, exclude={'SPY'})
    assert calls['tickers'] == {'ZZTA', 'ZZTB'}
    assert out == {'SPY': 150_000.0, 'ZZTA': 20_000.0, 'ZZTB': 15_000.0}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_cap_exemptions.py -q`
Expected: FAIL — SPY capped at 60k; `exclude` kwarg unexpected.

- [ ] **Step 3: Implement**

Per-ticker cap loop — add the skip as the first statement in the loop body:

```python
        for _tkr, _usd in list(target_usd.items()):
            if _tkr in _bench_tkrs:
                continue            # spec D6: no beta cap
            _gs = gate_net_sharpe.get(_tkr)
```

Cluster cap call site:

```python
    target_usd = _apply_asset_corr_cap(target_usd, gate_net_sharpe, nav,
                                       lam=lam_global, exclude=_bench_tkrs)
```

`_apply_asset_corr_cap`:

```python
def _apply_asset_corr_cap(target_usd, conviction, nav, lam=1.0, exclude=None):
    """… (keep the existing docstring) …
    exclude (spec 2026-08-29 D6): tickers removed from the set BEFORE
    clustering and re-inserted untouched after — benchmark tickers never
    consume cluster budget and never cause alpha releases."""
    apply_on, corr_thr, cap_pct = _load_asset_corr_cfg()
    shadow_on = os.environ.get('OPENCLAW_ASSET_CORR_CAP_SHADOW') == '1'
    if not (apply_on or shadow_on):
        return target_usd
    exclude = set(exclude or ())
    kept = {t: v for t, v in target_usd.items() if t in exclude}
    work = {t: v for t, v in target_usd.items() if t not in exclude}
    if not work:
        return target_usd
    try:
        from execution import asset_correlation as _ac
        from execution import asset_correlation_filter as _acf
        window = int(os.environ.get('OPENCLAW_ASSET_CORR_WINDOW', '63'))
        corr = _ac.price_return_corr(list(work), window=window)
        capped, audit = _acf.cap_correlated_clusters(
            work, conviction, corr, nav, cap_pct=cap_pct, corr_thr=corr_thr, lam=lam)
        logger.info(
            'asset_corr_cap.%s: thr=%.2f cap_pct=%.2f lam=%.2f cap_usd=%.0f excluded=%s '
            'clusters>=2=%d gross %.0f->%.0f released=%.0f %s',
            'apply' if apply_on else 'shadow', corr_thr, cap_pct, lam,
            cap_pct * lam * nav, sorted(exclude),
            sum(1 for c in audit['clusters'] if len(c['members']) >= 2),
            audit['total_gross_before'], audit['total_gross_after'], audit['released_usd'],
            [(c['members'], round(c['gross_before']), round(c['gross_after']))
             for c in audit['clusters'] if c['released'] or c['trimmed']][:10])
        if not apply_on:
            return target_usd
        out = dict(capped)
        out.update(kept)
        return out
    except Exception as e:
        logger.warning('asset_corr_cap failed (%s); fail-open', e)
        return target_usd
```

`tests/execution/test_sizer_per_ticker_cap.py:232`: change `def spy(target_usd, conviction, nav, lam=1.0):` to `def spy(target_usd, conviction, nav, lam=1.0, **kw):`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_cap_exemptions.py tests/execution/test_sizer_per_ticker_cap.py tests/execution/test_sizer_benchmark_hurdle_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_benchmark_cap_exemptions.py tests/execution/test_sizer_per_ticker_cap.py
git commit -m "sizer: benchmark tickers exempt from per-ticker cap and excluded from cluster capping (spec D6)"
git push origin main
```

---

### Task 12: Similarity — return-correlation only for benchmark-sleeve pairs

**Files:**
- Modify: `src/execution/strategy_similarity.py` (`blend_similarity` `:130-146`; `similarity_for_regime` `:354-366`; `similarity_for_regime_backtest` `:370-416`; `rebuild` `:507-548`)
- Create: `tests/execution/test_similarity_benchmark_pairs.py`

**Interfaces:**
- Produces: `blend_similarity(overlap, return_corr, n_obs_per_pair, bench_ids=None)`; `similarity_for_regime(..., bench_ids=None)`; `similarity_for_regime_backtest(..., bench_ids=None)`; `rebuild` loads `bench_ids` once via `execution.benchmark_sleeve.load_benchmark_sleeve_ids()` and passes it through.

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_similarity_benchmark_pairs.py
"""Spec D9: a pair containing a benchmark sleeve uses return-correlation only
once it has >= ALPHA_FULL_OBS overlapping observations; otherwise the normal blend."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import execution.strategy_similarity as ss  # noqa: E402

OVERLAP = {'S_beta_spy': {'S_beta_spy': 1.0, 'S_timer': 0.05, 'S_pairs': 0.0},
           'S_timer':    {'S_beta_spy': 0.05, 'S_timer': 1.0, 'S_pairs': 0.2},
           'S_pairs':    {'S_beta_spy': 0.0, 'S_timer': 0.2, 'S_pairs': 1.0}}
RET = {'S_beta_spy': {'S_beta_spy': 1.0, 'S_timer': 0.7, 'S_pairs': -0.1},
       'S_timer':    {'S_beta_spy': 0.7, 'S_timer': 1.0, 'S_pairs': 0.3},
       'S_pairs':    {'S_beta_spy': -0.1, 'S_timer': 0.3, 'S_pairs': 1.0}}


def _nobs(n):
    return {(a, b): n for a in OVERLAP for b in OVERLAP if a != b}


def test_benchmark_pair_uses_return_corr_only_at_full_obs():
    out = ss.blend_similarity(OVERLAP, RET, _nobs(200), bench_ids={'S_beta_spy'})
    assert out['S_beta_spy']['S_timer'] == 0.7 and out['S_timer']['S_beta_spy'] == 0.7
    assert out['S_beta_spy']['S_pairs'] == -0.1
    # a non-benchmark pair keeps the blend: 0.4*0.2 + 0.6*0.3
    assert abs(out['S_timer']['S_pairs'] - (0.4 * 0.2 + 0.6 * 0.3)) < 1e-12


def test_thin_history_keeps_the_blend_for_benchmark_pairs():
    out = ss.blend_similarity(OVERLAP, RET, _nobs(30), bench_ids={'S_beta_spy'})
    al = ss.adaptive_alpha(30)
    assert abs(out['S_beta_spy']['S_timer'] - ((1 - al) * 0.05 + al * 0.7)) < 1e-12


def test_no_bench_ids_is_byte_identical():
    a = ss.blend_similarity(OVERLAP, RET, _nobs(200))
    b = ss.blend_similarity(OVERLAP, RET, _nobs(200), bench_ids=set())
    assert a == b
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/execution/test_similarity_benchmark_pairs.py -q`
Expected: FAIL — unexpected kwarg `bench_ids`.

- [ ] **Step 3: Implement**

```python
def blend_similarity(overlap: dict[str, dict[str, float]],
                     return_corr: dict[str, dict[str, float]],
                     n_obs_per_pair: dict[tuple, int],
                     bench_ids: set | None = None) -> dict[str, dict[str, float]]:
    """Per-pair convex blend: (1-alpha)*overlap + alpha*return_corr, alpha=adaptive_alpha(n_obs).
    Overlap LEADS; return-corr enters only as joint history accrues. Diagonal 1.0.
    bench_ids (spec 2026-08-29 D9): for a pair with a benchmark sleeve on either
    side and n_obs >= ALPHA_FULL_OBS, the similarity is the return-correlation
    ALONE — the co-firing leg reads a single-ticker beta sleeve as near-orthogonal
    to everything and would over-credit diversification on the SPY long side."""
    bench_ids = set(bench_ids or ())
    strats = sorted(overlap.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for a in strats:
        for b in strats:
            if a == b:
                out[a][b] = 1.0
                continue
            o = overlap.get(a, {}).get(b, 0.0)
            r = return_corr.get(a, {}).get(b, SPARSE_DEFAULT)
            n = n_obs_per_pair.get((a, b), 0)
            if bench_ids and (a in bench_ids or b in bench_ids) and n >= ALPHA_FULL_OBS:
                out[a][b] = r
                continue
            al = adaptive_alpha(n)
            out[a][b] = (1.0 - al) * o + al * r
    return out
```

Add `bench_ids=None` as a trailing keyword to `similarity_for_regime` and `similarity_for_regime_backtest` and pass `bench_ids=bench_ids` into their `blend_similarity(...)` calls. In `rebuild`, after `src = resolve_source(source)`: 

```python
    from execution.benchmark_sleeve import load_benchmark_sleeve_ids
    bench_ids = load_benchmark_sleeve_ids()
```

and pass `bench_ids=bench_ids` at both call sites (`:541` and `:545`). The third caller at `:805` (`--shadow` report) also passes `bench_ids=load_benchmark_sleeve_ids()`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/execution/test_similarity_benchmark_pairs.py tests/execution/test_shrinkage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_similarity.py tests/execution/test_similarity_benchmark_pairs.py
git commit -m "similarity: benchmark-sleeve pairs use return-correlation only at full history (spec D9)"
git push origin main
```

---

### Task 13: Read-only replay: today's book OFF vs ON

**Files:**
- Create: `scripts/bench_relative_sizing_replay.py`
- Create: `tests/scripts/test_bench_relative_sizing_replay.py`

**Interfaces:**
- Produces: `diff_books(off: dict[str, float], on: dict[str, float], bench: set[str]) -> dict` with keys `dropped`, `added`, `moves` (list of `(ticker, off_usd, on_usd, delta)` sorted by `|delta|` desc), `gross_off`, `gross_on`, `beta_off`, `beta_on`; CLI `python3 scripts/bench_relative_sizing_replay.py --nav <equity> [--regime LOW_VOL]`.
- Consumes: `regime_blended_sizer.size_positions` with the real DB loaders (read-only), broker positions stubbed to `{}`, Discord posts stubbed, `OPENCLAW_EOD_RECONCILE=1`, `OPENCLAW_INTRADAY_REDEPLOY=0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_bench_relative_sizing_replay.py
import importlib.util
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('bench_replay', ROOT / 'scripts' / 'bench_relative_sizing_replay.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_diff_books():
    off = {'SPY': 60_000.0, 'ZZTA': 80_000.0, 'ZZTB': 60_000.0}
    on = {'SPY': 150_000.0, 'ZZTA': 50_000.0}
    d = mod.diff_books(off, on, {'SPY'})
    assert d['dropped'] == ['ZZTB'] and d['added'] == []
    assert d['moves'][0] == ('SPY', 60_000.0, 150_000.0, 90_000.0)
    assert d['gross_off'] == 200_000.0 and d['gross_on'] == 200_000.0
    assert abs(d['beta_off'] - 0.3) < 1e-9 and abs(d['beta_on'] - 0.75) < 1e-9
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/scripts/test_bench_relative_sizing_replay.py -q`
Expected: FAIL — file missing.

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
"""bench_relative_sizing_replay.py — size today's book twice, flag OFF and ON,
and print the per-ticker diff. READ-ONLY: no DB writes, no broker calls, no
Discord posts (all posting hooks are stubbed; broker positions are stubbed to
{} so orders == targets). Spec §2.5.2 — this is the parity artefact for rule C.

Run outside 13:00–20:15 UTC. Usage:
    python3 scripts/bench_relative_sizing_replay.py --nav 152000 [--regime LOW_VOL]
--nav is required (read it with: /root/go/bin/alpaca account get --jq .equity).
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def diff_books(off: dict, on: dict, bench: set) -> dict:
    g0 = sum(abs(v) for v in off.values()); g1 = sum(abs(v) for v in on.values())
    b0 = (sum(abs(v) for t, v in off.items() if t in bench) / g0) if g0 else 0.0
    b1 = (sum(abs(v) for t, v in on.items() if t in bench) / g1) if g1 else 0.0
    moves = sorted(((t, off.get(t, 0.0), on.get(t, 0.0), on.get(t, 0.0) - off.get(t, 0.0))
                    for t in set(off) | set(on)), key=lambda r: -abs(r[3]))
    return {'dropped': sorted(set(off) - set(on)), 'added': sorted(set(on) - set(off)),
            'moves': moves, 'gross_off': g0, 'gross_on': g1, 'beta_off': b0, 'beta_on': b1}


def _load_env():
    for line in (ROOT / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _regime_params(regime: str) -> dict:
    import psycopg2, psycopg2.extras
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
        with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute('SELECT * FROM regime_sizer_params WHERE regime_state = %s', (regime,))
            row = cur.fetchone()
            return dict(row) if row else {}


def _current_regime() -> str:
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
        with c.cursor() as cur:
            cur.execute('SELECT state FROM intraday_regime_states ORDER BY ts_utc DESC LIMIT 1')
            row = cur.fetchone()
            return row[0] if row else 'LOW_VOL'


def _size(nav: float, regime: str, flag_on: bool) -> dict:
    import execution.regime_blended_sizer as _sizer
    from execution import benchmark_sizing as bz
    os.environ['OPENCLAW_EOD_RECONCILE'] = '1'
    os.environ['OPENCLAW_INTRADAY_REDEPLOY'] = '0'
    os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '0'
    if flag_on: os.environ[bz.BENCH_SIZING_ENV] = '1'
    else:       os.environ.pop(bz.BENCH_SIZING_ENV, None)
    _sizer._load_broker_positions_usd = lambda: {}
    _sizer._post_corr_cumsharpe_log = lambda line: None
    _sizer._post_flatten_alert = lambda *a, **k: None
    _sizer._post_ops_alert = lambda *a, **k: None
    _sizer._maybe_flatten_zero_conviction = lambda *a, **k: None
    account = {'equity': nav, 'regt_buying_power': 2 * nav, 'long_market_value': 0, 'cash': nav}
    orders = _sizer.size_positions(signals=[], account_state=account, regime={'state': regime},
                                   run_date=date.today(), strategy_state={},
                                   regime_params=_regime_params(regime), confirmer=lambda p: {})
    return {o['ticker']: float(o['target_usd']) for o in orders
            if o.get('action') not in ('close_long', 'close_short') and 'target_usd' in o}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--nav', type=float, required=True)
    ap.add_argument('--regime', default=None)
    ap.add_argument('--top', type=int, default=25)
    a = ap.parse_args(argv)
    _load_env()
    from execution import benchmark_sleeve as bsl
    regime = a.regime or _current_regime()
    bench_ids = bsl.load_benchmark_sleeve_ids()
    off = _size(a.nav, regime, False)
    on = _size(a.nav, regime, True)
    # Benchmark tickers = the beta sleeve's ticker when the registry flags it.
    bench = ({'SPY'} & (set(off) | set(on))) if 'S_beta_spy' in bench_ids else set()
    d = diff_books(off, on, bench)
    print(f'regime={regime} nav={a.nav:.0f} bench_ids={sorted(bench_ids)} bench_tickers={sorted(bench)}')
    print(f"gross OFF={d['gross_off']:.0f} ON={d['gross_on']:.0f}  beta_share OFF={d['beta_off']:.3f} ON={d['beta_on']:.3f}")
    print(f"dropped ({len(d['dropped'])}): {d['dropped']}")
    print(f"added   ({len(d['added'])}): {d['added']}")
    print(f"{'ticker':10s} {'OFF':>12s} {'ON':>12s} {'delta':>12s}")
    for t, o, n, dl in d['moves'][:a.top]:
        print(f'{t:10s} {o:12.0f} {n:12.0f} {dl:12.0f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run test + a syntax check**

Run: `python3 -m pytest tests/scripts/test_bench_relative_sizing_replay.py -q && python3 -m py_compile scripts/bench_relative_sizing_replay.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/bench_relative_sizing_replay.py tests/scripts/test_bench_relative_sizing_replay.py
git commit -m "scripts: read-only replay of today's book with rule C OFF vs ON (spec §2.5.2)"
git push origin main
```

---

### Task 14: Register + backtest + promote `S_beta_spy` (ops)

**Files:** none committed (registry row, backtest run rows, manifest write by the promotion tooling — the manifest diff stays unstaged).

- [ ] **Step 1: Register as a candidate with the flag mirrored into `parameters`**

```bash
cd /root/openclaw/src && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' ../.env | cut -d= -f2- | tr -d '"')" python3 - <<'EOF'
import sys, json; sys.path.insert(0, '.')
from execution.strategy_weights import _db
from strategies.implementations.S_beta_spy import BetaSpy, STRATEGY_ID
assert BetaSpy.benchmark_sleeve is True
c = _db(); cur = c.cursor()
cur.execute("""
INSERT INTO strategy_registry (id, name, description, tier, implementation_path, parameters, universe, signal_frequency, status)
VALUES (%s, %s, %s, 2, %s, %s::jsonb, ARRAY['SPY'], 'daily', 'pending_approval')
ON CONFLICT (id) DO UPDATE SET parameters = strategy_registry.parameters || EXCLUDED.parameters
""", (STRATEGY_ID, BetaSpy.name, BetaSpy.description,
      'src/strategies/implementations/S_beta_spy.py', json.dumps({'benchmark_sleeve': True, 'instrument_class': 'etp'})))
c.commit()
cur.execute("select id, status, parameters from strategy_registry where id=%s", (STRATEGY_ID,)); print(cur.fetchone())
EOF
```
Expected: `('S_beta_spy', 'pending_approval', {'benchmark_sleeve': True, 'instrument_class': 'etp'})`. Then `python3 -c "…load_benchmark_sleeve_ids()…"` returns `{'S_beta_spy'}`.

- [ ] **Step 2: Smoke the backtest for one month (SPY must be in the resolved panel)**

```bash
cd /root/openclaw && PYTHONPATH=src POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" nice -n 19 python3 -m backtest.unified_backtest --strategy-file src/strategies/implementations/S_beta_spy.py --universe-cap tier_liquid --start-date 2026-06-01 --end-date 2026-07-31 2>&1 | tail -15
```
Expected: `total_trades` ≈ 40 (one per bar), no `signals=0 (no SPY column)`. If SPY is absent from the panel, stop and report — the universe cap must include the benchmark before the full run.

- [ ] **Step 3: Full backtest as a transient unit (evening, after 20:15 UTC)**

```bash
cd /root/openclaw && systemd-run --unit=beta-spy-backtest --nice=19 -p CPUQuota=100% -p MemoryMax=3500M \
  -p WorkingDirectory=/root/openclaw -E PYTHONPATH=/root/openclaw/src -E PYTHONUNBUFFERED=1 \
  -E NUMEXPR_MAX_THREADS=1 -E OMP_NUM_THREADS=1 \
  -E POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" \
  /usr/bin/python3 -m backtest.unified_backtest --strategy-file src/strategies/implementations/S_beta_spy.py --universe-cap tier_liquid
journalctl -u beta-spy-backtest -f
```
Expected on completion: one `strategy_backtest_runs` row `primary_window=true` for `S_beta_spy`; sleeves ≈ LOW_VOL 1.9–2.0 / TRANSITIONING ~0.55 / HIGH_VOL ~0.6 / CRISIS ~0.7, each with `trade_count` ≥ 100; `benchmark_sharpe` populated. Verify:

```bash
cd /root/openclaw/src && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' ../.env | cut -d= -f2- | tr -d '"')" python3 -c "
import sys; sys.path.insert(0,'.')
from execution.strategy_weights import _db
c=_db().cursor(); c.execute(\"select x.regime_state, round(x.sharpe::numeric,2), x.trade_count, round(x.max_dd_pct::numeric,1), round(x.benchmark_sharpe::numeric,2) from strategy_backtest_regimes x join strategy_backtest_runs y on y.run_id=x.run_id where y.strategy_id='S_beta_spy' and y.primary_window order by 1\"); print(c.fetchall())"
```

- [ ] **Step 4: Promote candidate → live through the normal lifecycle**

Use the :3000 dashboard Strategies tab → `S_beta_spy` → Promote to live (the route at `src/channels/api/server.js:2017` calls `transitionStrategy`, which runs the sleeve gate — Sharpe > 0, DD/Calmar, trades ≥ 100 — registry-first then manifest). Expected: registry `status='approved'`; the LOW_VOL sleeve qualifies. Then confirm the daily cycle's activation step made it eligible in LOW_VOL only and the weights rebuild picked it up:

```bash
cd /root/openclaw/src && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' ../.env | cut -d= -f2- | tr -d '"')" python3 -c "
import sys; sys.path.insert(0,'.')
from execution.strategy_weights import _db
c=_db().cursor()
c.execute(\"select regime_state, eligible from strategy_regime_params where strategy_id='S_beta_spy' order by 1\"); print(c.fetchall())
c.execute(\"select regime_state, round(daily_weight::numeric,3) from strategy_weights_by_regime where strategy_id='S_beta_spy' and is_current\"); print(c.fetchall())"
```
Expected: `eligible` True only for LOW_VOL; one weights row, `daily_weight ≈ 1.9–2.0`. If the activation step has not run yet, trigger it: `cd /root/openclaw && PYTHONPATH=src POSTGRES_URI=… python3 -m execution.activation_apply --trigger=manual` (see `src/execution/activation_apply.py` docstring for the exact flag names before running).

- [ ] **Step 5: Next 15:00 ET cycle — verify the sleeve reaches the sizer**

`journalctl --user -u johnbot.service --since "today 18:50" | grep -E "bench_sleeve|bench_sizing.shadow"` — expect `bench_sleeve: 1 benchmark ticker(s) ['SPY'] …` and the shadow line with `bench=['SPY']`. The flag is still OFF, so SPY is sized on raw S_adj (already exempt from the acting gate and both caps — Tasks 8/11 are live as soon as the sleeve is).

---

### Task 15: Shadow soak and flip (ops)

- [ ] **Step 1: Read two consecutive daily shadow lines**

`journalctl --user -u johnbot.service --since "-3 days" | grep "bench_sizing.shadow"` — for each: `S_m`, `dropped=N/M`, `beta_share_before/after`, `gross_moved_frac`, `top_moves`. Record them in `/root/.learnings/LEARNINGS.md`.

- [ ] **Step 2: Replay diff (outside the lane)**

```bash
cd /root/openclaw && NAV=$(/root/go/bin/alpaca account get --jq .equity) && PYTHONPATH=src nice -n 19 python3 scripts/bench_relative_sizing_replay.py --nav "$NAV"
```
Expected: the same `dropped` set and beta share as the shadow line; SPY's ON target ≈ `beta_share × λ·NAV`, uncapped.

- [ ] **Step 3: Flip**

```bash
cd /root/openclaw && printf '\n# 2026-08-29 spec §2.5: benchmark-relative sizing (S_adj − S_m). 1 = apply; unset = shadow only.\nOPENCLAW_BENCH_RELATIVE_SIZING=1\n' >> .env
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service
sleep 5; XDG_RUNTIME_DIR=/run/user/0 systemctl --user is-active johnbot.service
```

- [ ] **Step 4: Verify at the next 15:00 ET cycle and at the broker**

`journalctl --user -u johnbot.service --since "today 18:50" | grep -E "bench_sizing.apply|per-ticker cap|asset_corr_cap"` — expect `bench_sizing.apply[LOW_VOL]: …`, no per-ticker cap line naming SPY, `excluded=['SPY']` on the cluster-cap line. Then `/root/go/bin/alpaca order list --symbols SPY --limit 500 --jq length` and `/root/go/bin/alpaca position get SPY` to confirm the SPY order/position matches the logged target (verify at the BROKER, never trust the heartbeat).

---

### Task 16: R1 assigner dry-run check (ops, after Task 1 lands)

- [ ] **Step 1: Confirm nothing but the two 08-26 strategies could change**

```bash
cd /root/openclaw && PYTHONPATH=src POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" nice -n 19 python3 -m backtest.activation_assigner --all --dry-run 2>&1 | grep -E "S_btc_halving_clock_cycle_timing|S_evar_tempered_stable_sector_etf|activated|deactivated" | head
```
Expected: zero `[bench_gate]` lines anywhere; any cell flip is on those two strategies only (they were the only live strategies whose runs carried `benchmark_sharpe`). Record the output in `/root/.learnings/LEARNINGS.md`.

---

### Task 17: Docs

**Files:**
- Modify: `docs/archive/changelog.md` (newest-first entry), `CLAUDE.md` (Current Projects line), `docs/specs/2026-08-29-benchmark-relative-sizing-spec.md` (Status line), `.env.example` (three flags documented)

- [ ] **Step 1: Changelog entry (top of file)**

```markdown
## 2026-08-29 — Benchmark-relative sizing (spec docs/specs/2026-08-29-benchmark-relative-sizing-spec.md)
- R1 SPY benchmark leg REMOVED from promotion, lifecycle and both assigners (D1). `benchmark_sharpe` column still written (informational).
- `strategy_weights.daily_weight = effective_sharpe` — √hold cadence normalization retired (`OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1` reverts). Trade-count factor OFF by default (`OPENCLAW_TRADE_WEIGHT_FACTOR=1` restores).
- New beta sleeve `S_beta_spy` (`benchmark_sleeve=True`, mirrored to `strategy_registry.parameters`). Benchmark tickers: exempt from `min_acting_strategies`, the hurdle, the per-ticker cap; excluded from cluster capping.
- Rule C in the sizer: alpha tickers sized on `sign(S_adj)·max(|S_adj| − S_m, 0)`; `S_m` = SPY regime Sharpe over 2016-04-11..today (`pipeline_config.benchmark_regime_sharpe`). `OPENCLAW_BENCH_RELATIVE_SIZING` (shadow line every cycle; `1` = apply). Replay: `scripts/bench_relative_sizing_replay.py`.
- Similarity: pairs with a benchmark sleeve use return-correlation only at ≥60 obs (D9).
```

- [ ] **Step 2: `CLAUDE.md` Current Projects** — append one bullet: `**Benchmark-relative sizing (2026-08-29)** — spec + plan landed; S_m sizes, never gates; beta sleeve S_beta_spy; flags: OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM (revert), OPENCLAW_TRADE_WEIGHT_FACTOR (revert), OPENCLAW_BENCH_RELATIVE_SIZING (apply). Status: …` — write the real state at commit time, one of `shadow soaking since <date>` or `flag flipped <date> <UTC time>` (from Task 15 Step 3).

- [ ] **Step 3: Spec Status line** → `Status: IMPLEMENTED <date> (plan docs/superpowers/plans/2026-08-29-benchmark-relative-sizing.md); flag flipped <date>` once Task 15 is done; until then `IMPLEMENTING`.

- [ ] **Step 4: `.env.example`** — add the three flags with one-line comments (values unset).

- [ ] **Step 5: Commit**

```bash
git add docs/archive/changelog.md CLAUDE.md docs/specs/2026-08-29-benchmark-relative-sizing-spec.md .env.example
git commit -m "docs: benchmark-relative sizing changelog, CLAUDE.md status, spec status, .env.example flags"
git push origin main
```

---

## Execution order and dependencies

1 → 2 → 3 → 4 → **5 (ops)** → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → **14 (ops)** → **15 (ops)** → 16 (ops, any time after 1) → 17.

Tasks 1–4 and 6–13 are code with tests and can be reviewed independently; 8, 10, 11 touch the same sizer function and must land in that order. Task 5 must run before Task 14 so the sleeve's first weights row is already un-normalized. Task 15's flip is the only step that changes live sizing behaviour beyond the weights rebuild in Task 5.
