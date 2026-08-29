# Benchmark-relative sizing — Amendment 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sizer's benchmark hurdle `S_m` a forward-looking, horizon-matched, rf-consistent SPY Sharpe (default H = 1), make the beta sleeve exit on regime flips via the exit hook and stay eligible in every regime regardless of the activation slider, and make the sleeve re-backtestable by the fleet runner.

**Architecture:** `backtest.benchmark_baseline` gains a horizon-grid estimator that applies the engine's own sleeve-Sharpe estimator (daily-marks union, rf 5 %) to synthetic benchmark lots entered at regime-tagged closes; `execution.benchmark_sizing` caches the grid (`schema: 2`) and selects the column named by `pipeline_config['benchmark_horizon_days']`. `S_beta_spy` opts into the existing exit hook with a one-line `regime_exit` rule. `unified_backtest.load_prices_panels` learns a pyarrow ticker filter driven by manifest `metadata.backtest_tickers`. `activation_assigner` / `strategy_weights` learn a `benchmark_sleeve` exemption (always eligible, never auto-demoted).

**Tech Stack:** Python 3 / pandas / pyarrow / pytest (`PYTHONPATH=src`), PostgreSQL (`pipeline_config`, migration SQL applied with `psql`), systemd transient units for the re-backtest.

**Spec:** `docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md` (commit `d2f8574`). Read §0 (findings), §1–§5 (decisions D-A*, D-B*, D-C*, D-D*, D-E*) before starting. The parent spec is `docs/specs/2026-08-29-benchmark-relative-sizing-spec.md`.

## Global Constraints

- Run Python tests with `PYTHONPATH=src python3 -m pytest <file>::<test> -q -p no:cacheprovider`. **Only the files named in each task — NEVER the full suite** (tests reach the real DB through `.env`; the fleet may be running).
- Never stage or commit `src/strategies/manifest.json`, `src/strategies/registry.py`, `src/strategies/strategy_signatures*.json`, `src/engine/daily-health-digest.js`, or any `src/strategies/implementations/S_g3m_*` / `S_lead_lag_*` file (research-pipeline-owned, pipeline-dirty). `git add` explicit paths only.
- Never `source .env`. Read a key with `grep -E '^KEY=' .env | cut -d= -f2-`.
- Never `git reset --hard`. Never delete master data. johnbot is user-scope: `XDG_RUNTIME_DIR=/run/user/0 systemctl --user …`; NEVER start the system unit.
- No heavy compute 13:00–20:15 UTC on weekdays; long compute runs as a transient systemd unit (`systemd-run --unit=… -p MemoryMax=3500M -p OOMScoreAdjust=1000 --nice=19`).
- Fail-open contracts stay fail-open: `regime_benchmark_sharpe*` returns `{}` on load failure and `None` per thin regime; the sizer block sizes on raw `S_adj` on any exception.
- Non-hook strategies stay byte-identical in the backtest; `tickers=None` keeps `load_prices_panels` byte-identical.
- Commit per task on `main`, `git push origin main` after each commit. Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT`.
- Numbers to expect (spec §0 F5, rf 5 %, H=1): LOW_VOL 0.80 / TRANSITIONING 0.41 / HIGH_VOL 0.49 / CRISIS 1.54.

## File structure

| file | responsibility |
|---|---|
| `src/backtest/benchmark_baseline.py` (modify) | `RISK_FREE_DAILY`, `BENCH_HORIZONS`, `_excess_sharpe`, `regime_benchmark_sharpe_by_horizon`, `regime_benchmark_sharpe` = H=1 column |
| `src/backtest/unified_backtest.py` (modify) | stale comment at the `benchmark_sharpe` persist (Task 1); `load_prices_panels(tickers=)` + `_manifest_backtest_tickers` (Task 4) |
| `src/execution/benchmark_sizing.py` (modify) | `CACHE_SCHEMA`, `HORIZON_KEY`, `load_benchmark_horizon`, schema-2 cache, `horizon=` kwarg, `shadow_line(h=)` |
| `src/execution/regime_blended_sizer.py` (modify) | rule-C block passes `horizon=_h` / `h=_h` |
| `src/database/migrations/151_benchmark_horizon_days.sql` (create) | seeds `benchmark_horizon_days`; refreshes the stale column comment |
| `src/strategies/implementations/S_beta_spy.py` (modify) | `exit_hook = True`, `should_exit` → `regime_exit` |
| `src/backtest/activation_assigner.py` (modify) | `always_on` in `compute_eligible` / `apply_one`, `rule=` in `_apply_regime`, sleeve ids in `main()` |
| `src/execution/strategy_weights.py` (modify) | `find_negative_across_all_eligible` excludes benchmark sleeves |
| `tests/backtest/test_benchmark_baseline.py` (modify) | Task 1 |
| `tests/execution/test_benchmark_sizing.py`, `tests/execution/conftest.py` (modify) | Task 2 |
| `tests/strategies/test_beta_spy_exit_hook.py` (create) | Task 3 |
| `tests/backtest/test_arrow_dictionary_read_equivalence.py` (modify), `tests/backtest/test_manifest_backtest_tickers.py` (create) | Task 4 |
| `tests/backtest/test_activation_assigner.py` (modify), `tests/execution/test_find_negative_benchmark_exclusion.py` (create) | Task 5 |
| `docs/archive/changelog.md`, `/root/CLAUDE.md`, memory, SDD ledger | Task 6 |

---

### Task 1: Forward, horizon-matched, rf-consistent `S_m` (`benchmark_baseline`)

**Files:**
- Modify: `src/backtest/benchmark_baseline.py` (module docstring; constants after `_LOOKBACK_CALENDAR_DAYS`; replace `regime_benchmark_sharpe` at the bottom of the file)
- Modify: `src/backtest/unified_backtest.py` (comment block above `_benchmark_sharpe_by_regime: dict[str, float] = {}`, ~line 1404–1415)
- Test: `tests/backtest/test_benchmark_baseline.py`

**Interfaces:**
- Consumes: existing `load_regime_tags(start_date, end_date) -> dict[str, str]`, `load_benchmark_closes(start_date, end_date, benchmark) -> dict[str, float]`, `CANONICAL_REGIMES`, `TRADING_DAYS_PER_YEAR`.
- Produces (Task 2 relies on these exact names):
  - `benchmark_baseline.RISK_FREE_ANNUAL = 0.05`, `benchmark_baseline.RISK_FREE_DAILY`
  - `benchmark_baseline.BENCH_HORIZONS = (1, 2, 3, 5, 10, 21)`, `benchmark_baseline.DEFAULT_HORIZON = 1`
  - `regime_benchmark_sharpe_by_horizon(start_date, end_date, benchmark: str = 'SPY', min_obs: int = 40, horizons=BENCH_HORIZONS) -> dict[str, dict[int, float | None]]` — `{}` on load failure; every canonical regime present; `None` per (regime, H) when < `min_obs` mark-days or zero variance.
  - `regime_benchmark_sharpe(start_date, end_date, benchmark='SPY', min_obs=40) -> dict[str, float | None]` — unchanged signature and flat shape; value = the `DEFAULT_HORIZON` (H=1) column; `{}` on load failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_benchmark_baseline.py` (keep everything above; the file's helpers `_write_regimes`, `_write_prices`, `_alternating_closes`, `BENCHMARK_TICKER`, `bb` are reused):

```python
# ── Amendment 1 (spec docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md §1) ──
import math


def _excess_sharpe_ref(xs):
    """Independent reference: excess (rf 5 %/252) annualized Sharpe, ddof=1."""
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return (m - 0.05 / 252) / sd * math.sqrt(252)


@pytest.fixture
def two_block_8(tmp_path, monkeypatch):
    """8 business days d0..d7; d0..d3 LOW_VOL, d4..d7 HIGH_VOL; close-to-close
    returns r1..r7 = +1, -1, +2, -2, +3, -3, +4 % (r_i is the return INTO d_i)."""
    dates = pd.bdate_range('2022-01-03', periods=8)
    rets = [None, .01, -.01, .02, -.02, .03, -.03, .04]
    closes = [100.0]
    for r in rets[1:]:
        closes.append(closes[-1] * (1 + r))
    regimes = ['LOW_VOL'] * 4 + ['HIGH_VOL'] * 4
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, closes)
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates, rets


def test_by_horizon_h1_is_the_next_day_return_set(two_block_8):
    dates, rets = two_block_8
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=1, horizons=(1, 2, 21))
    # LOW_VOL entries at the closes of d0..d3 -> H=1 mark-days d1..d4 -> r1..r4
    assert out['LOW_VOL'][1] == pytest.approx(_excess_sharpe_ref(rets[1:5]))
    # H=2 -> mark-days d1..d5 (union of overlapping 2-day lots)
    assert out['LOW_VOL'][2] == pytest.approx(_excess_sharpe_ref(rets[1:6]))
    # H=21 truncates at end of data -> d1..d7
    assert out['LOW_VOL'][21] == pytest.approx(_excess_sharpe_ref(rets[1:8]))
    # HIGH_VOL entries d4..d7 -> H=1 mark-days d5..d7 (d8 does not exist)
    assert out['HIGH_VOL'][1] == pytest.approx(_excess_sharpe_ref(rets[5:8]))
    # never tagged -> None at every H, but the regime keys are still present
    assert out['TRANSITIONING'] == {1: None, 2: None, 21: None}
    assert out['CRISIS'] == {1: None, 2: None, 21: None}


def test_forward_h1_differs_from_the_old_contemporaneous_statistic(two_block_8):
    dates, rets = two_block_8
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=1, horizons=(1,))
    # pre-amendment: returns ON the tagged days d1..d3 (d0 has no return) = r1..r3
    contemporaneous = _excess_sharpe_ref(rets[1:4])
    assert out['LOW_VOL'][1] != pytest.approx(contemporaneous)


def test_flat_wrapper_returns_the_h1_column(two_block_8):
    dates, _ = two_block_8
    by_h = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=1)
    flat = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=1)
    assert set(flat) == set(bb.CANONICAL_REGIMES)
    for r in bb.CANONICAL_REGIMES:
        assert flat[r] == by_h[r][bb.DEFAULT_HORIZON]
    assert bb.DEFAULT_HORIZON == 1 and bb.BENCH_HORIZONS == (1, 2, 3, 5, 10, 21)


def test_min_obs_counts_mark_days_per_horizon(two_block_8):
    dates, _ = two_block_8
    # LOW_VOL has 4 mark-days at H=1 and 5 at H=2: min_obs=5 nulls H=1 only
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=5, horizons=(1, 2))
    assert out['LOW_VOL'][1] is None and out['LOW_VOL'][2] is not None


def test_by_horizon_load_failure_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic parquet read failure')
    monkeypatch.setattr(bb, 'load_regime_tags', _raise)
    assert bb.regime_benchmark_sharpe_by_horizon('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


def test_risk_free_constant_matches_the_engine():
    from backtest import unified_backtest as ub
    from execution import trade_handoff_builder as thb
    assert bb.RISK_FREE_ANNUAL == 0.05
    assert bb.RISK_FREE_DAILY == pytest.approx(ub.RISK_FREE_DAILY)
    assert bb.RISK_FREE_DAILY == pytest.approx(thb.RISK_FREE_DAILY)
```

Then REPLACE the body of the existing `test_regime_benchmark_sharpe_near_zero_both_regimes_and_untagged_are_none` (its "abs(...) < 0.5" bound was calibrated for rf = 0) with an exact reference and change the `alternating_252` fixture to also return the closes and regimes:

```python
@pytest.fixture
def alternating_252(tmp_path, monkeypatch):
    """252 business days, six alternating 42-day LOW_VOL/HIGH_VOL blocks
    (TRANSITIONING/CRISIS never tagged); ZZT_SPY alternates +-1% daily."""
    dates = pd.bdate_range('2020-01-02', periods=252)
    block = 42
    labels = ['LOW_VOL', 'HIGH_VOL']
    regimes = [labels[(i // block) % 2] for i in range(len(dates))]
    closes = _alternating_closes(len(dates))
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, closes)
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates, closes, regimes


def _ref_h1(closes, regimes, regime):
    """Reference H=1 statistic: the return INTO the day after each tagged close."""
    rets = [None] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    xs = [rets[i + 1] for i in range(len(closes) - 1) if regimes[i] == regime]
    return _excess_sharpe_ref(xs)


def test_regime_benchmark_sharpe_matches_reference_and_untagged_are_none(alternating_252):
    dates, closes, regimes = alternating_252
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['LOW_VOL'] == pytest.approx(_ref_h1(closes, regimes, 'LOW_VOL'))
    assert out['HIGH_VOL'] == pytest.approx(_ref_h1(closes, regimes, 'HIGH_VOL'))
    # Never tagged in this fixture -> 0 mark-days -> thin -> None.
    assert out['TRANSITIONING'] is None
    assert out['CRISIS'] is None
```

(`_excess_sharpe_ref` must be defined above this test — put the "Amendment 1" helper block right after the existing `_alternating_closes` helper, before the fixtures.) `test_thin_regime_10_tagged_days_is_none` stays as is: 10 CRISIS closes give 10 mark-days (< 40 → None); the 50 LOW_VOL closes give 49 mark-days (≥ 40).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_benchmark_baseline.py -q -p no:cacheprovider`
Expected: the new tests FAIL with `AttributeError: module ... has no attribute 'regime_benchmark_sharpe_by_horizon'` / `RISK_FREE_ANNUAL`; the reference test fails on value.

- [ ] **Step 3: Implement**

In `src/backtest/benchmark_baseline.py`, after `_LOOKBACK_CALENDAR_DAYS = 10` add:

```python
# Amendment 1 (spec docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md D-A1..A3).
# rf mirrors unified_backtest.RISK_FREE_DAILY (declared locally: this module is
# deliberately import-free of the backtest engine; equality is unit-tested).
RISK_FREE_ANNUAL = 0.05
RISK_FREE_DAILY = RISK_FREE_ANNUAL / TRADING_DAYS_PER_YEAR
# Horizon grid (trading days a synthetic benchmark lot is held). The sizer
# selects one column via pipeline_config['benchmark_horizon_days'] (default 1).
BENCH_HORIZONS = (1, 2, 3, 5, 10, 21)
DEFAULT_HORIZON = 1
```

Replace the whole `regime_benchmark_sharpe` function (from `def regime_benchmark_sharpe(` to end of file) with:

```python
def _excess_sharpe(rets: list[float], min_obs: int) -> float | None:
    """(mean − rf_daily) / std(ddof=1) · √252 — the estimator
    unified_backtest.aggregate_metrics applies to a sleeve's daily-marks
    equity curve. None when thin (< min_obs) or degenerate (zero variance)."""
    n = len(rets)
    if n < max(min_obs, 2):
        return None
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    std = math.sqrt(var)
    if std <= 1e-9:
        return None
    return (mean - RISK_FREE_DAILY) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def regime_benchmark_sharpe_by_horizon(start_date, end_date,
                                       benchmark: str = "SPY",
                                       min_obs: int = 40,
                                       horizons=BENCH_HORIZONS) -> dict[str, dict[int, float | None]]:
    """Forward, entry-tagged benchmark Sharpe per (regime, horizon).

    For each canonical regime and each H in `horizons`: a synthetic lot of
    `benchmark` is entered at the close of EVERY day tagged with that regime
    and held exactly H trading days (no stop/target/cost). The statistic is
    the engine's sleeve estimator (see _excess_sharpe) over the benchmark's
    close-to-close return on every trading day on which at least one such
    lot is open (the equal-weight daily-marks union — identical lots make the
    equal-weight average the plain return). For H = 1 the day set is exactly
    {t+1 : regime(t) = R}: the return a close-of-day decision can capture.

    Pre-amendment this module scored the return ON the tagged day (same-day
    VIX tag ⇒ selection on the outcome, corr(SPY ret, ΔVIX) ≈ −0.79) with
    rf = 0; that statistic (LOW_VOL ≈ 2.0) is not tradeable and is gone.

    Returns {} on any load failure (logged) — callers fail open. Every
    canonical regime is present in the result; a (regime, H) with fewer than
    `min_obs` mark-days (or zero variance) is None.
    """
    try:
        tags = load_regime_tags(start_date, end_date)
        closes = load_benchmark_closes(start_date, end_date, benchmark)
    except Exception as e:
        logger.warning("[bench_baseline] benchmark load failed: %s: %s", type(e).__name__, e)
        return {}
    if not tags or not closes:
        logger.warning("[bench_baseline] benchmark load returned no data "
                        "(regime_tags=%d benchmark_closes=%d)", len(tags), len(closes))
        return {}

    dates = sorted(closes)
    n = len(dates)
    # rets[i] = close-to-close return INTO dates[i]; None for the first day / bad closes.
    rets: list[float | None] = [None] * n
    for i in range(1, n):
        p0, p1 = closes[dates[i - 1]], closes[dates[i]]
        if p0 and p0 == p0 and p1 == p1 and p0 > 0:
            rets[i] = p1 / p0 - 1.0

    hs = sorted({int(h) for h in horizons if int(h) >= 1})
    out: dict[str, dict[int, float | None]] = {r: {} for r in CANONICAL_REGIMES}
    for regime in CANONICAL_REGIMES:
        entries = [i for i, d in enumerate(dates) if tags.get(d) == regime]
        for h in hs:
            marked: set[int] = set()
            for i in entries:
                for k in range(1, h + 1):
                    j = i + k
                    if j >= n:
                        break
                    marked.add(j)
            xs = [rets[j] for j in sorted(marked) if rets[j] is not None]
            out[regime][h] = _excess_sharpe(xs, min_obs)
    return out


def regime_benchmark_sharpe(start_date, end_date,
                            benchmark: str = "SPY",
                            min_obs: int = 40) -> dict[str, float | None]:
    """Flat {regime: Sharpe | None} = the DEFAULT_HORIZON (H = 1) column of
    regime_benchmark_sharpe_by_horizon. Signature and shape unchanged for
    unified_backtest's informational strategy_backtest_regimes.benchmark_sharpe
    write. {} on load failure."""
    by_h = regime_benchmark_sharpe_by_horizon(start_date, end_date, benchmark=benchmark,
                                              min_obs=min_obs, horizons=(DEFAULT_HORIZON,))
    if not by_h:
        return {}
    return {r: (by_h.get(r) or {}).get(DEFAULT_HORIZON) for r in CANONICAL_REGIMES}
```

Update the module docstring's first paragraph to: "The module computes the benchmark's (SPY) forward, entry-tagged excess Sharpe (rf 5 %) per regime and holding horizon — the engine's own sleeve estimator applied to synthetic benchmark lots (Amendment 1, 2026-08-29). Since 2026-08-29 its ONLY consumer is sizing …" (keep the rest of the docstring).

In `src/backtest/unified_backtest.py`, replace the stale comment block that begins `# Benchmark-relative promotion criterion (task R1, 2026-08-24` and ends `# try/except only guards the import + call plumbing.` with:

```python
        # strategy_backtest_regimes.benchmark_sharpe (migration 149) — INFORMATIONAL
        # since 2026-08-29 (the R1 gate leg was removed from promotion/activation;
        # nothing reads it as a gate). Value = benchmark_baseline.regime_benchmark_sharpe
        # = SPY's next-day (H=1) excess Sharpe after closes tagged with the regime,
        # over this run's window (Amendment 1 spec D-A2). Computed ONCE per run.
        # try/except non-fatal — a missing/broken benchmark must never fail a
        # backtest run (fail-open contract; see benchmark_baseline module docstring).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_benchmark_baseline.py tests/backtest/test_no_benchmark_gate.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/backtest/benchmark_baseline.py src/backtest/unified_backtest.py tests/backtest/test_benchmark_baseline.py && git commit -q -m "feat(bench): forward entry-tagged S_m by horizon, rf 5% (amendment 1 D-A1..A4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main
```

---

### Task 2: Schema-2 cache, `benchmark_horizon_days`, sizer wiring, migration 151

**Files:**
- Modify: `src/execution/benchmark_sizing.py` (constants; `_write_cache` description; `regime_benchmark_sharpe_for_sizing`; `shadow_line`; new `load_benchmark_horizon`)
- Modify: `src/execution/regime_blended_sizer.py` (rule-C block in `_sharpe_cadence_path`: the two `_bsz.` calls)
- Create: `src/database/migrations/151_benchmark_horizon_days.sql`
- Modify: `tests/execution/conftest.py` (autouse stub list), `tests/execution/test_benchmark_sizing.py`

**Interfaces:**
- Consumes (Task 1): `backtest.benchmark_baseline.regime_benchmark_sharpe_by_horizon`, `BENCH_HORIZONS`, `DEFAULT_HORIZON`.
- Produces:
  - `benchmark_sizing.CACHE_SCHEMA = 2`, `benchmark_sizing.HORIZON_KEY = 'benchmark_horizon_days'`
  - `load_benchmark_horizon(default: int = 1, conn=None) -> int` — value of `pipeline_config[HORIZON_KEY]` if it is an int on `BENCH_HORIZONS`; else `default` (logged).
  - `regime_benchmark_sharpe_for_sizing(regime_state, run_date, *, benchmark='SPY', conn=None, compute=None, horizon: int | None = None) -> float | None` — `compute` now has the by-horizon signature `(start, end, benchmark=..., min_obs=...) -> {regime: {H: v}}`; `horizon=None` ⇒ `load_benchmark_horizon(conn=conn)`.
  - Cache payload: `{'schema': 2, 'as_of', 'benchmark', 'start', 'horizons': [1,2,3,5,10,21], 'by_regime': {regime: {'1': v, …}}}` (H keys are strings — JSON).
  - `shadow_line(..., mode='shadow', h: int | None = None)` — emits ` h=<H>` right after `S_m=…` when `h` is given.

- [ ] **Step 1: Write the failing tests**

In `tests/execution/test_benchmark_sizing.py` REPLACE `test_computes_persists_and_reuses_cache` and `test_thin_regime_and_failures_return_none` with, and append the horizon/shadow tests:

```python
def _grid(**cols):
    """{regime: {H: v}} from per-regime lists over the grid (1,2,3,5,10,21)."""
    hs = (1, 2, 3, 5, 10, 21)
    return {r: dict(zip(hs, vals)) for r, vals in cols.items()}


def test_computes_persists_schema2_and_reuses_cache():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append((start, end, benchmark))
        return _grid(LOW_VOL=[0.80, 0.60, 0.51, 0.57, 0.34, 0.26],
                     TRANSITIONING=[0.41, 0.25, 0.29, 0.42, 0.52, 0.20],
                     HIGH_VOL=[0.49, 0.59, 0.87, 1.11, 0.63, 0.73],
                     CRISIS=[None] * 6)
    store = {}
    conn = _Conn(store)
    v = bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute)
    assert v == 0.80 and calls == [('2016-04-11', '2026-08-31', 'SPY')]
    payload = json.loads(store[bz.CONFIG_KEY])
    assert payload['schema'] == bz.CACHE_SCHEMA == 2
    assert payload['horizons'] == [1, 2, 3, 5, 10, 21]
    assert payload['by_regime']['LOW_VOL']['5'] == 0.57 and payload['by_regime']['CRISIS']['1'] is None
    # same day: cache hit, no recompute; explicit horizon selects a column
    assert bz.regime_benchmark_sharpe_for_sizing('HIGH_VOL', date(2026, 8, 31), conn=conn,
                                                 compute=compute, horizon=5) == 1.11
    assert len(calls) == 1
    # a new day recomputes
    bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 9, 1), conn=conn, compute=compute)
    assert len(calls) == 2


def test_schema1_cache_is_a_miss_and_is_rewritten():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append(1)
        return _grid(LOW_VOL=[0.80] * 6, TRANSITIONING=[0.41] * 6, HIGH_VOL=[0.49] * 6, CRISIS=[1.54] * 6)
    store = {bz.CONFIG_KEY: json.dumps({'as_of': '2026-08-31', 'benchmark': 'SPY', 'start': '2016-04-11',
                                        'by_regime': {'LOW_VOL': 2.01, 'TRANSITIONING': 0.6,
                                                      'HIGH_VOL': 0.6, 'CRISIS': 0.73}})}
    conn = _Conn(store)
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute) == 0.80
    assert calls == [1]
    assert json.loads(store[bz.CONFIG_KEY])['schema'] == 2


def test_horizon_config_selects_column_and_falls_back_to_1():
    compute = lambda *a, **k: _grid(LOW_VOL=[0.80, 0.60, 0.51, 0.57, 0.34, 0.26], TRANSITIONING=[None] * 6,
                                    HIGH_VOL=[None] * 6, CRISIS=[None] * 6)
    for raw, expected in [('5', 0.57), ('21', 0.26), (None, 0.80), ('7', 0.80), ('abc', 0.80), ('1.0', 0.80)]:
        store = {}
        if raw is not None:
            store[bz.HORIZON_KEY] = raw
        conn = _Conn(store)
        assert bz.load_benchmark_horizon(conn=conn) == (int(float(raw)) if raw in ('5', '21', '1.0') else 1)
        assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute) == expected


def test_thin_regime_and_failures_return_none():
    conn = _Conn({})
    assert bz.regime_benchmark_sharpe_for_sizing('CRISIS', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: _grid(CRISIS=[None] * 6)) is None
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: {}) is None
    def boom(*a, **k): raise RuntimeError('parquet gone')
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn, compute=boom) is None


def test_shadow_line_carries_horizon():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 0.8, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 0.8, before, after, dropped, {'SPY'}, lam_nav=100_000.0, h=1)
    assert line.startswith("bench_sizing.shadow[LOW_VOL]: S_m=0.80 h=1 bench=['SPY']")
    # h omitted -> byte-identical to the pre-amendment format
    assert bz.shadow_line('LOW_VOL', 0.8, before, after, dropped, {'SPY'}, 100_000.0).startswith(
        "bench_sizing.shadow[LOW_VOL]: S_m=0.80 bench=['SPY']")
```

Note: the `_Cur` fake in this file answers a SELECT with `(store.get(params[0]),)` — so a `store[bz.HORIZON_KEY] = '5'` entry is what `load_benchmark_horizon` reads; a missing key returns `None` → default.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_benchmark_sizing.py -q -p no:cacheprovider`
Expected: FAIL (`AttributeError: ... CACHE_SCHEMA`, `HORIZON_KEY`, `load_benchmark_horizon`; unexpected kwarg `horizon`/`h`).

- [ ] **Step 3: Implement `benchmark_sizing.py`**

Constants (after `CONFIG_KEY = 'benchmark_regime_sharpe'`):

```python
CACHE_SCHEMA = 2                      # Amendment 1: by-horizon grid; schema-1 (contemporaneous) payloads are a miss
HORIZON_KEY = 'benchmark_horizon_days'
DEFAULT_HORIZON = 1
```

New function (place after `_write_cache`):

```python
def load_benchmark_horizon(default: int = DEFAULT_HORIZON, conn=None) -> int:
    """pipeline_config[HORIZON_KEY] as an int on benchmark_baseline.BENCH_HORIZONS.
    Absent, unparseable or off-grid -> `default` (logged). Mirrors
    regime_blended_sizer._load_lambda's read-with-fallback pattern."""
    from backtest.benchmark_baseline import BENCH_HORIZONS
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (HORIZON_KEY,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return int(default)
        raw = str(row[0]).strip().strip('"')
        h = int(float(raw))
        if h not in BENCH_HORIZONS or float(raw) != h:
            logger.warning('[bench_sizing] %s=%r is not on the grid %s; using %d',
                           HORIZON_KEY, row[0], BENCH_HORIZONS, default)
            return int(default)
        return h
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %d',
                       HORIZON_KEY, type(e).__name__, e, default)
        return int(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
```

Rewrite `regime_benchmark_sharpe_for_sizing`:

```python
def regime_benchmark_sharpe_for_sizing(regime_state: str, run_date, *, benchmark: str = 'SPY',
                                       conn=None, compute=None, horizon: int | None = None):
    """S_m for regime_state as of run_date at horizon H, or None. Reuses the
    pipeline_config cache when it is schema-2 and its as_of == run_date (the
    5-minute intraday lane must not re-read the parquet); otherwise computes the
    whole (regime × horizon) grid, persists it, returns the selected column.
    H = `horizon` or pipeline_config['benchmark_horizon_days'] (default 1)."""
    as_of = run_date.strftime('%Y-%m-%d') if hasattr(run_date, 'strftime') else str(run_date)[:10]
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cached = _read_cache(cur)
        if (cached and cached.get('schema') == CACHE_SCHEMA
                and cached.get('as_of') == as_of and cached.get('benchmark') == benchmark):
            by_regime = cached.get('by_regime') or {}
        else:
            from backtest.unified_backtest import DEFAULT_START_DATE
            from backtest.benchmark_baseline import BENCH_HORIZONS
            if compute is None:
                from backtest.benchmark_baseline import regime_benchmark_sharpe_by_horizon as compute
            by_h = compute(DEFAULT_START_DATE, as_of, benchmark=benchmark) or {}
            by_regime = {r: {str(int(h)): (float(v) if v is not None else None)
                             for h, v in (by_h.get(r) or {}).items()}
                         for r in CANONICAL_REGIMES}
            if any(v is not None for hv in by_regime.values() for v in hv.values()):
                _write_cache(conn, {'schema': CACHE_SCHEMA, 'as_of': as_of, 'benchmark': benchmark,
                                    'start': DEFAULT_START_DATE, 'horizons': list(BENCH_HORIZONS),
                                    'by_regime': by_regime})
            else:
                logger.warning('[bench_sizing] S_m compute returned no regimes for %s..%s',
                               DEFAULT_START_DATE, as_of)
                return None
        h = int(horizon) if horizon is not None else load_benchmark_horizon(conn=conn)
        v = (by_regime.get(regime_state) or {}).get(str(h))
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
```

`_write_cache` description string → `'Benchmark (SPY) forward entry-tagged excess Sharpe (rf 5%) by regime × horizon (schema 2, amendment 1 2026-08-29) used by the sizer hurdle S_adj − S_m; column selected by pipeline_config.benchmark_horizon_days. Refreshed once per run_date by the sizer; window = unified_backtest.DEFAULT_START_DATE .. as_of.'`

`shadow_line`: add `h: int | None = None` after `mode`, and change the return's first f-string to:

```python
    h_txt = '' if h is None else f' h={int(h)}'
    return (f'bench_sizing.{mode}[{regime_state}]: S_m={s_m_txt}{h_txt} bench={sorted(bench_tickers)} '
            f'dropped={len(dropped)}/{len(before)} beta_share_before={beta0:.3f} beta_share_after={beta1:.3f} '
            f'gross_moved_frac={moved:.3f} dropped_tickers={sorted(dropped)[:15]} top_moves={moves[:10]}')
```

Module docstring: replace the sentence "S_m is the benchmark's (SPY) annualized Sharpe over the days tagged with the sizer's regime-of-record, computed by backtest.benchmark_baseline over the canonical fleet window …" with "S_m is the benchmark's (SPY) forward, entry-tagged excess Sharpe (rf 5 %, the engine's sleeve estimator) after closes tagged with the sizer's regime-of-record, held `benchmark_horizon_days` trading days (default 1 = the daily decision cadence), computed by backtest.benchmark_baseline.regime_benchmark_sharpe_by_horizon over the canonical fleet window (unified_backtest.DEFAULT_START_DATE .. run_date) so it is unit-for-unit with the sleeve Sharpes S_adj is built from (Amendment 1, 2026-08-29)."

- [ ] **Step 4: Wire the sizer**

In `src/execution/regime_blended_sizer.py`, inside the rule-C `try:` block of `_sharpe_cadence_path`, change

```python
        _s_m = _bsz.regime_benchmark_sharpe_for_sizing(regime_state, date.today())
```
to
```python
        _h = _bsz.load_benchmark_horizon()
        _s_m = _bsz.regime_benchmark_sharpe_for_sizing(regime_state, date.today(), horizon=_h)
```
and the `shadow_line` call to end with `mode='apply' if _apply_hurdle else 'shadow', h=_h)`. Update the block's leading comment ("Rule C — benchmark-relative sizing (spec 2026-08-29 §2.5)…") by appending one line: `# Amendment 1: S_m is forward/entry-tagged at horizon pipeline_config.benchmark_horizon_days (default 1).`

- [ ] **Step 5: Keep sizer tests DB-free**

In `tests/execution/conftest.py`, next to the existing two `monkeypatch.setattr(...)` stubs (inside the same `if request.path.name not in (...)` guard), add:

```python
        monkeypatch.setattr('execution.benchmark_sizing.load_benchmark_horizon', lambda *a, **k: 1)
```
and extend that comment: "load_benchmark_horizon (Amendment 1) also opens a connection on the 5-min lane; stubbed to 1."

- [ ] **Step 6: Migration 151**

Create `src/database/migrations/151_benchmark_horizon_days.sql`:

```sql
-- 151: benchmark-relative sizing, amendment 1 (2026-08-29;
-- docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md D-A2/D-A6).
-- Seeds the sizer's benchmark horizon (trading days a synthetic SPY lot is held
-- when computing S_m). Grid 1,2,3,5,10,21 is cached by the sizer in
-- pipeline_config.benchmark_regime_sharpe (schema 2); this key picks the column.
-- Default 1 = the system's daily decision cadence (operator ruling 2026-08-29).
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('benchmark_horizon_days', '1',
        'Amendment 1 2026-08-29: horizon H (trading days) selecting the S_m column the sizer hurdle S_adj - S_m uses. Must be one of 1,2,3,5,10,21 (off-grid -> 1, logged). 1 = daily decision cadence.',
        NOW())
ON CONFLICT (key) DO NOTHING;

-- Migration 149's column comment described the retired contemporaneous rf=0
-- statistic and two gate readers that were removed on 2026-08-29.
COMMENT ON COLUMN strategy_backtest_regimes.benchmark_sharpe IS
  'Amendment 1 2026-08-29: SPY next-day (H=1) excess Sharpe (rf 5%) after closes tagged with this regime, over the run''s [start_date, end_date] window (src/backtest/benchmark_baseline.py regime_benchmark_sharpe). INFORMATIONAL only — no gate reads it since 2026-08-29. NULL = thin regime (<40 mark-days) or load failure.';
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_benchmark_sizing.py tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_sizer_benchmark_cap_exemptions.py tests/execution/test_sizer_benchmark_acting_gate.py -q -p no:cacheprovider`
Expected: all PASS. Also `python3 -c "import ast,sys; ast.parse(open('src/execution/regime_blended_sizer.py').read())"` exits 0.

- [ ] **Step 8: Commit** (the migration is applied in Task 6's runbook, not here)

```bash
cd /root/openclaw && git add src/execution/benchmark_sizing.py src/execution/regime_blended_sizer.py src/database/migrations/151_benchmark_horizon_days.sql tests/execution/conftest.py tests/execution/test_benchmark_sizing.py && git commit -q -m "feat(bench): schema-2 horizon-grid S_m cache, benchmark_horizon_days config, sizer h= wiring (amendment 1 D-A5..A7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main
```

---

### Task 3: Beta sleeve exits on regime flip (exit hook)

**Files:**
- Modify: `src/strategies/implementations/S_beta_spy.py`
- Test: `tests/strategies/test_beta_spy_exit_hook.py` (create)

**Interfaces:**
- Consumes: `strategies.base.BaseStrategy` (`exit_hook` class flag; `__init_subclass__` requires a `should_exit` override when `exit_hook = True`), `strategies.base.CANONICAL_REGIMES`, `backtest.open_book.OpenTrade` / `advance_open_book(book, current_date, bars_by_ticker, panel, regime, aux, strategy, *, dt_priority, counters)`.
- Produces: `BetaSpy.exit_hook = True`; `BetaSpy.should_exit(position, prices, regime, aux_data=None) -> 'regime_exit' | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/strategies/test_beta_spy_exit_hook.py`:

```python
"""Amendment 1 D-B1..B3: S_beta_spy exits on any regime change via the exit hook."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import CANONICAL_REGIMES
from strategies.implementations.S_beta_spy import BetaSpy, HOLD_DAYS
from backtest.open_book import OpenTrade, advance_open_book


def _pos(entry_regime):
    return {'ticker': 'SPY', 'direction': 'LONG', 'entry_price': 500.0,
            'entry_date': pd.Timestamp('2026-01-05'), 'days_held': 3,
            'stop_loss': 300.0, 'target_1': 2500.0,
            'signal_params': {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': entry_regime}}


def test_flag_and_hold_unchanged():
    assert BetaSpy.exit_hook is True
    assert HOLD_DAYS == 21            # promotion hold-cap parity guard (D-B2)


@pytest.mark.parametrize('entry', CANONICAL_REGIMES)
def test_same_regime_holds_other_regime_exits(entry):
    s = BetaSpy()
    for state in CANONICAL_REGIMES:
        got = s.should_exit(_pos(entry), pd.DataFrame(), {'state': state})
        assert got == (None if state == entry else 'regime_exit')


def test_missing_or_unknown_state_holds():
    s = BetaSpy()
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {'state': 'UNKNOWN'}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), None) is None
    p = _pos('LOW_VOL'); p['signal_params'] = {}
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None
    p['signal_params'] = None
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None


def test_signal_records_entry_regime():
    dates = pd.date_range('2026-01-05', periods=5, freq='B')
    prices = pd.DataFrame({'SPY': [500.0, 501.0, 502.0, 503.0, 504.0]}, index=dates)
    sig = BetaSpy().generate_signals(prices, {'state': 'HIGH_VOL'}, ['SPY'])
    assert len(sig) == 1 and sig[0].signal_params['regime'] == 'HIGH_VOL'


def test_open_book_closes_on_the_flip_bar():
    dates = pd.date_range('2026-01-05', periods=12, freq='B')
    closes = [500.0 + i for i in range(12)]
    bars = pd.DataFrame({'open': closes, 'high': [c + 0.5 for c in closes],
                         'low': [c - 0.5 for c in closes], 'close': closes},
                        index=pd.DatetimeIndex(dates, name='date'))
    panel = pd.DataFrame({'SPY': closes}, index=dates)
    regimes = ['LOW_VOL'] * 6 + ['HIGH_VOL'] * 6          # flip on dates[6]
    strat = BetaSpy()
    trade = OpenTrade(ticker='SPY', direction=1, entry_date=dates[0], entry_price=500.0,
                      entry_fill=500.0, stop_loss=300.0, target_1=2500.0, hold_cap=HOLD_DAYS,
                      entry_regime='LOW_VOL',
                      signal_params={'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': 'LOW_VOL'},
                      slippage=0.0, prev_mark=500.0)
    book, closed_all, counters = [trade], [], {}
    for i, d in enumerate(dates[1:], start=1):
        closed = advance_open_book(book, d, {'SPY': bars}, panel.loc[:d],
                                   {'state': regimes[i], 'date': d.date().isoformat()},
                                   {'options': {}}, strat, dt_priority='stop', counters=counters)
        closed_all.extend(closed)
        if not book:
            break
    assert len(closed_all) == 1 and book == []
    t = closed_all[0]
    assert t['exit_reason'] == 'strategy_exit:regime_exit'
    assert t['exit_date'] == dates[6].date()
    assert t['holding_days'] == 6
    assert counters['hook_exits'] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_beta_spy_exit_hook.py -q -p no:cacheprovider`
Expected: FAIL (`exit_hook is False`; `should_exit` returns None for every state; open-book run ends by `max_hold`, not on dates[6]).

- [ ] **Step 3: Implement**

In `src/strategies/implementations/S_beta_spy.py`:

1. Import: change `from strategies.base import BaseStrategy, Signal` to `from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES`.
2. Class attributes: add `exit_hook = True` after `benchmark_sleeve = True`.
3. Add the method after `generate_signals`:

```python
    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Amendment 1 D-B1: flatten at today's close when the regime-of-record
        differs from the lot's entry regime (recorded in signal_params at
        signal time), so the sleeve's per-regime Sharpe measures beta WHILE the
        regime holds — the same quantity S_m measures at H=1. Any missing or
        non-canonical state on either side => hold (the hold cap still
        protects). Pure: no price reads."""
        state = regime.get('state') if isinstance(regime, dict) else None
        entry = ((position or {}).get('signal_params') or {}).get('regime')
        if state not in CANONICAL_REGIMES or entry not in CANONICAL_REGIMES:
            return None
        return 'regime_exit' if state != entry else None
```

4. Module docstring: replace the sentence beginning "the activation slider makes it dormant in regimes where SPY does not clear the slider" through "(execution.benchmark_sizing.apply_benchmark_hurdle)." with: "it is eligible in EVERY regime regardless of the activation slider (activation_assigner `benchmark_sleeve_always_on`, Amendment 1 D-D1) and sized on its own regime sleeve; alpha tickers are hurdled against S_m (execution.benchmark_sizing.apply_benchmark_hurdle), which since Amendment 1 is the forward H=1 SPY Sharpe — no longer derived from this strategy's run." Also replace "Signal shape: ONE LONG SPY per bar, hold_days = 21 …" paragraph's last sentence "Stop/targets are set so they never bind — the sleeve carries no bracket edge to protect." with "Stop/targets are set so they never bind. Exit hook (Amendment 1 D-B1): the lot is flattened on the first bar whose regime differs from its entry regime and re-opened next bar tagged with the new regime; live, `write_signals`' continuation mint keeps the position (no churn), the backtest pays one spread per flip." Update the class `description` string to `'Benchmark sleeve: long SPY every bar, hold 21, exits on regime flip; eligible in all regimes.'`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_beta_spy_exit_hook.py tests/strategies/test_exit_hook_interface.py tests/backtest/test_open_book.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/strategies/implementations/S_beta_spy.py tests/strategies/test_beta_spy_exit_hook.py && git commit -q -m "feat(beta-sleeve): exit hook regime_exit on regime flip (amendment 1 D-B1..B3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main
```

---

### Task 4: Fleet-backtestable sleeve — `load_prices_panels(tickers=…)` from manifest `backtest_tickers`

**Files:**
- Modify: `src/backtest/unified_backtest.py` (`load_prices_panels` signature + the `pq.read_table` call; new `_manifest_backtest_tickers` next to `_bounded_resolver`; the `load_prices_panels(...)` call in `run_backtest`)
- Test: `tests/backtest/test_arrow_dictionary_read_equivalence.py` (extend), `tests/backtest/test_manifest_backtest_tickers.py` (create)
- Manifest: `src/strategies/manifest.json` — `S_beta_spy.metadata.backtest_tickers = ["SPY"]` (edit through the manifest lock; NOT committed — pipeline-owned)

**Interfaces:**
- Produces: `load_prices_panels(calendar: str = 'union', tickers=None)`; `_manifest_backtest_tickers(strategy_id: str, *, manifest_path=None) -> list[str] | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_arrow_dictionary_read_equivalence.py` (reuse its `_write_synthetic` and the existing equivalence helper pattern; read the file's existing test to copy how it monkeypatches `ub.PRICES_PARQUET` and `qf` and calls `ub.load_prices_panels`):

```python
def test_tickers_filter_equals_unfiltered_slice(tmp_path, monkeypatch):
    path = tmp_path / 'prices.parquet'
    _write_synthetic(path)
    # same neutralisation as test_arrow_read_matches_old_pandas_read above
    monkeypatch.setattr(qf, '_cached', lambda master_table: set())
    monkeypatch.setattr(ub, 'PRICES_PARQUET', path)
    cw_all, bars_all = ub.load_prices_panels()
    cw_one, bars_one = ub.load_prices_panels(tickers=['AAPL'])
    assert list(cw_one.columns) == ['AAPL']
    assert cw_one.equals(cw_all[['AAPL']]), "filtered close_wide not bit-identical to the sliced full panel"
    assert set(bars_one) == {'AAPL'}
    assert bars_one['AAPL'].equals(bars_all['AAPL'])
    # two tickers, given unsorted with a duplicate -> sorted unique columns
    cw_two, bars_two = ub.load_prices_panels(tickers=['MSFT', 'AA', 'MSFT'])
    assert list(cw_two.columns) == ['AA', 'MSFT'] and set(bars_two) == {'AA', 'MSFT'}
    assert cw_two.equals(cw_all[['AA', 'MSFT']])
    # tickers=None / empty list are the unfiltered path (byte-identical)
    assert ub.load_prices_panels(tickers=None)[0].equals(cw_all)
    assert ub.load_prices_panels(tickers=[])[0].equals(cw_all)
```

(`qf._cached` is exactly how `test_arrow_read_matches_old_pandas_read` neutralises the quarantine filter; `pq.read_table(..., read_dictionary=[...], filters=[('ticker','in',[...])])` was verified on this box to return dictionary-typed ticker/date columns, so the categorical normalisation below the read is unchanged.)

Create `tests/backtest/test_manifest_backtest_tickers.py`:

```python
"""Amendment 1 D-C2: manifest metadata.backtest_tickers -> load_prices_panels(tickers=)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


def _manifest(tmp_path, entry):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'S_x': entry}}))
    return p


def test_reads_sorted_unique_symbols(tmp_path):
    p = _manifest(tmp_path, {'state': 'live', 'metadata': {'backtest_tickers': ['SPY', 'QQQ', 'SPY', '']}})
    assert ub._manifest_backtest_tickers('S_x', manifest_path=p) == ['QQQ', 'SPY']


def test_absent_or_invalid_is_none(tmp_path):
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': []}})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': 'SPY'}})) is None
    assert ub._manifest_backtest_tickers('S_other', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=tmp_path / 'missing.json') is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_arrow_dictionary_read_equivalence.py tests/backtest/test_manifest_backtest_tickers.py -q -p no:cacheprovider`
Expected: FAIL (`TypeError: load_prices_panels() got an unexpected keyword argument 'tickers'`; `AttributeError: _manifest_backtest_tickers`).

- [ ] **Step 3: Implement**

`load_prices_panels` signature → `def load_prices_panels(calendar: str = 'union', tickers=None) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:` and extend its docstring with: "`tickers`: optional collection of symbols; when given, the parquet read is filtered to those tickers via a pyarrow predicate (Amendment 1 D-C1 — lets a single-ticker strategy such as S_beta_spy run inside the fleet's memory cap). None = the full panel, byte-identical to before."

Replace the read line

```python
    _tbl = pq.read_table(PRICES_PARQUET, columns=_COLS, read_dictionary=['ticker', 'date'])
```
with
```python
    _filters = None
    if tickers:
        _wanted = sorted({str(t) for t in tickers if t})
        _filters = [('ticker', 'in', _wanted)] if _wanted else None
    _tbl = pq.read_table(PRICES_PARQUET, columns=_COLS, read_dictionary=['ticker', 'date'],
                         filters=_filters)
```

Add after `_bounded_resolver`:

```python
def _manifest_backtest_tickers(strategy_id: str, *, manifest_path=None):
    """Amendment 1 D-C2: manifest metadata.backtest_tickers -> sorted unique
    symbol list, or None (absent, empty, not a list, unreadable manifest, or
    unregistered strategy). Feeds load_prices_panels(tickers=)."""
    manifest_path = Path(manifest_path or ROOT / 'src' / 'strategies' / 'manifest.json')
    try:
        entry = (json.loads(manifest_path.read_text()).get('strategies', {})
                 .get(strategy_id) or {})
        raw = (entry.get('metadata') or {}).get('backtest_tickers')
    except Exception:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    out = sorted({str(t) for t in raw if t})
    return out or None
```

In `run_backtest`, change

```python
    close_wide, bars_by_ticker = load_prices_panels(calendar=_calendar_for(instrument_class))
```
to
```python
    _bt_tickers = _manifest_backtest_tickers(strategy_id)
    if _bt_tickers:
        _log(f'prices: manifest backtest_tickers={_bt_tickers} (filtered panel read)')
    close_wide, bars_by_ticker = load_prices_panels(calendar=_calendar_for(instrument_class),
                                                    tickers=_bt_tickers)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_arrow_dictionary_read_equivalence.py tests/backtest/test_manifest_backtest_tickers.py tests/backtest/test_unified_backtest_panel_hook.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Manifest edit (uncommitted, through the lock)**

Run from `/root/openclaw` (only when no pipeline step is running: `systemctl list-units --state=running | grep -E 'openclaw|johnbot' ` shows no `openclaw-*` compute unit):

```bash
node -e "
const path=require('path');
const {withManifestLock}=require(path.join(process.cwd(),'src/lib/manifest_lock'));
const MP=path.join(process.cwd(),'src/strategies/manifest.json');
withManifestLock(MP, async (m)=>{ const e=m.strategies['S_beta_spy']; e.metadata=e.metadata||{}; e.metadata.backtest_tickers=['SPY']; console.log('backtest_tickers set'); }).then(()=>process.exit(0)).catch(e=>{console.error(e.message);process.exit(1);});
"
python3 -c "import json; print(json.load(open('src/strategies/manifest.json'))['strategies']['S_beta_spy']['metadata']['backtest_tickers'])"
```
Expected: `['SPY']`. If the classifier blocks `node -e`, apply the same one-key change with the Edit tool on `src/strategies/manifest.json` (add `"backtest_tickers": ["SPY"]` inside `S_beta_spy.metadata`). Do NOT `git add` the manifest.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/backtest/unified_backtest.py tests/backtest/test_arrow_dictionary_read_equivalence.py tests/backtest/test_manifest_backtest_tickers.py && git commit -q -m "feat(backtest): load_prices_panels(tickers=) from manifest backtest_tickers (amendment 1 D-C1..C3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main
```

---

### Task 5: Always-on sleeve activation + auto-demote exclusion

**Files:**
- Modify: `src/backtest/activation_assigner.py` (`compute_eligible`, `_apply_regime`, `apply_one`, `main`)
- Modify: `src/execution/strategy_weights.py` (`find_negative_across_all_eligible`)
- Test: `tests/backtest/test_activation_assigner.py` (extend), `tests/execution/test_find_negative_benchmark_exclusion.py` (create)

**Interfaces:**
- Consumes: `execution.benchmark_sleeve.PARAM_KEY = 'benchmark_sleeve'`, `execution.benchmark_sleeve.load_benchmark_sleeve_ids(conn=None) -> set[str]`.
- Produces: `compute_eligible(conn, strategy_id, threshold, min_trades=None, instrument_class='equity', *, always_on=False)`; `apply_one(conn, strategy_id, threshold, dry_run=False, min_trades=None, instrument_class='equity', *, always_on=False)`; `_apply_regime(..., instrument_class='equity', rule='qualifies(>0·classDD·trades)+slider')`; audit reason `... rule=benchmark_sleeve_always_on` for sleeves.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_activation_assigner.py` (uses its `FakeConn`, `FakeCursor`, `_regime_row`, `_prior_row` helpers; copy the response-list convention from `TestComputeEligible`'s existing tests — the first `execute` answers the primary-run lookup, the second the regime rows):

```python
class TestBenchmarkSleeveAlwaysOn(unittest.TestCase):
    def _conn(self):
        # responses (same convention as TestComputeEligible): primary_window run
        # lookup -> {'run_id': ...}; universe_shrink_metrics chosen rows -> [] (none);
        # then the 4 strategy_backtest_regimes rows
        rows = [_regime_row('LOW_VOL', 0.20, 920), _regime_row('TRANSITIONING', 0.16, 1129),
                _regime_row('HIGH_VOL', 0.69, 411), _regime_row('CRISIS', 1.15, 150)]
        return FakeConn([{'run_id': 'r1'}, [], rows])

    def test_always_on_makes_every_regime_eligible_but_keeps_diag(self):
        elig, diag = aa.compute_eligible(self._conn(), 'S_beta_spy', threshold=1.0,
                                         instrument_class='etp', always_on=True)
        self.assertEqual(elig, {r: True for r in aa.CANONICAL_REGIMES})
        self.assertFalse(diag['LOW_VOL']['eligible'])      # slider verdict still recorded
        self.assertTrue(diag['CRISIS']['eligible'])

    def test_default_is_unchanged(self):
        elig, _ = aa.compute_eligible(self._conn(), 'S_beta_spy', threshold=1.0, instrument_class='etp')
        self.assertEqual(elig, {'LOW_VOL': False, 'TRANSITIONING': False, 'HIGH_VOL': False, 'CRISIS': True})

    def test_no_run_is_still_skipped(self):
        elig, diag = aa.compute_eligible(FakeConn([None]), 'S_beta_spy', threshold=1.0, always_on=True)
        self.assertIsNone(elig)
        self.assertEqual(diag, {})

    def test_apply_regime_reason_names_the_rule(self):
        cur = FakeCursor([None, None])
        aa._apply_regime(cur, 'S_beta_spy', 'LOW_VOL', True, {}, sharpe=0.2, trade_count=920,
                         threshold=1.0, min_trades=100, max_dd_pct=35.1, instrument_class='etp',
                         rule='benchmark_sleeve_always_on')
        reasons = [p for sql, p in cur.executed if 'strategy_regime_param_changes' in sql]
        self.assertTrue(any('rule=benchmark_sleeve_always_on' in str(p) for p in reasons))
        cur2 = FakeCursor([None, None])
        aa._apply_regime(cur2, 'S_y', 'LOW_VOL', True, {}, sharpe=1.2, trade_count=300,
                         threshold=1.0, min_trades=100)
        self.assertTrue(any('rule=qualifies(>0·classDD·trades)+slider' in str(p)
                            for sql, p in cur2.executed if 'strategy_regime_param_changes' in sql))
```

If `compute_eligible`'s primary-run lookup in this file's existing tests uses a different first response shape (check `TestComputeEligible.test_sharpe_threshold_boundary`), mirror that shape exactly.

Create `tests/execution/test_find_negative_benchmark_exclusion.py`:

```python
"""Amendment 1 D-D2: benchmark sleeves are never auto-demote candidates."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_weights as sw  # noqa: E402


class _Cur:
    def __init__(self, sleeve_ids):
        self.sleeve_ids = sleeve_ids
        self._rows = []
    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        if 'strategy_registry' in s:
            self._rows = [(i,) for i in self.sleeve_ids]
        else:
            self._rows = []                      # no positive regimes, no closed history
    def __iter__(self):
        return iter(self._rows)
    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, sleeve_ids):
        self._cur = _Cur(sleeve_ids)
    def cursor(self):
        return self._cur
    def close(self):
        pass


def _manifest(tmp_path, monkeypatch):
    root = tmp_path
    (root / 'src' / 'strategies').mkdir(parents=True)
    (root / 'src' / 'strategies' / 'manifest.json').write_text(json.dumps({'strategies': {
        'S_beta_spy': {'state': 'live', 'history': []},
        'S_alpha': {'state': 'live', 'history': []},
    }}))
    monkeypatch.setattr(sw, 'ROOT', root)
    monkeypatch.setattr(sw, '_strategies_in_grace_period', lambda manifest, grace_days: set())


def test_sleeve_excluded_alpha_still_demotable(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    out = sw.find_negative_across_all_eligible(conn=_Conn({'S_beta_spy'}), grace_days=0)
    assert out == ['S_alpha']


def test_no_sleeves_is_the_old_behaviour(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    out = sw.find_negative_across_all_eligible(conn=_Conn(set()), grace_days=0)
    assert sorted(out) == ['S_alpha', 'S_beta_spy']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_activation_assigner.py tests/execution/test_find_negative_benchmark_exclusion.py -q -p no:cacheprovider`
Expected: FAIL (`TypeError: unexpected keyword argument 'always_on'` / `'rule'`; `test_sleeve_excluded_alpha_still_demotable` returns both ids).

- [ ] **Step 3: Implement `activation_assigner.py`**

`compute_eligible` signature → `def compute_eligible(conn, strategy_id: str, threshold: float, min_trades: Optional[int] = None, instrument_class: str = 'equity', *, always_on: bool = False) -> tuple[Optional[dict], dict]:` and add to its docstring: "always_on (Amendment 1 D-D1): benchmark sleeves are eligible in every canonical regime regardless of the slider; `diag[..]['eligible']` still records the slider verdict per regime." Replace the final two lines

```python
    eligible_by_regime = {r: diag.get(r, {}).get('eligible', False) for r in CANONICAL_REGIMES}
    return eligible_by_regime, diag
```
with
```python
    if always_on:
        eligible_by_regime = {r: True for r in CANONICAL_REGIMES}
    else:
        eligible_by_regime = {r: diag.get(r, {}).get('eligible', False) for r in CANONICAL_REGIMES}
    return eligible_by_regime, diag
```

`_apply_regime` signature → append `rule: str = 'qualifies(>0·classDD·trades)+slider'` after `instrument_class: str = 'equity'`, and change the reason's last line `f'rule=qualifies(>0·classDD·trades)+slider')` to `f'rule={rule}')`.

`apply_one` signature → `def apply_one(conn, strategy_id: str, threshold: float, dry_run: bool = False, min_trades: Optional[int] = None, instrument_class: str = 'equity', *, always_on: bool = False) -> dict:`; pass `always_on=always_on` to `compute_eligible`; in the write loop pass `rule=('benchmark_sleeve_always_on' if always_on else 'qualifies(>0·classDD·trades)+slider')` to `_apply_regime`.

`main()`: after `classes = _load_instrument_classes()` add

```python
    # Amendment 1 D-D1: benchmark sleeves (registry parameters.benchmark_sleeve=true)
    # are eligible in every regime regardless of the slider. Fail-open to "no
    # sleeves" (the loader logs); rollback keeps the transaction clean either way.
    try:
        from execution.benchmark_sleeve import load_benchmark_sleeve_ids
        bench_ids = load_benchmark_sleeve_ids(conn)
    except Exception as e:
        _log(f'benchmark sleeve lookup failed ({e}); no always-on strategies this run')
        bench_ids = set()
    try:
        conn.rollback()
    except Exception:
        pass
    if bench_ids:
        _log(f'always_on (benchmark sleeves): {sorted(bench_ids)}')
```
and in the loop `apply_one(conn, sid, threshold, dry_run=args.dry_run, min_trades=resolved_min_trades, instrument_class=classes.get(sid, 'equity'), always_on=(sid in bench_ids))`.

Module docstring (top of file, the "Rule" paragraph): add one line "Benchmark sleeves (registry `parameters.benchmark_sleeve`) are always eligible in all four regimes (Amendment 1 D-D1) — the slider never touches them."

- [ ] **Step 4: Implement `strategy_weights.py`**

In `find_negative_across_all_eligible`, right after `active_ids = [...]` and its `if not active_ids: return []`, insert:

```python
        # Amendment 1 D-D2: a benchmark sleeve is never auto-demoted — its
        # dormancy in a bad regime is expressed by weight, not by state. Read
        # through the same cursor/transaction as the sets below (a registry
        # failure fails this whole function like any other DB error here).
        from execution.benchmark_sleeve import PARAM_KEY as _BENCH_PARAM_KEY
        cur = conn.cursor()
        cur.execute("SELECT id FROM strategy_registry WHERE (parameters ->> %s) = 'true'",
                    (_BENCH_PARAM_KEY,))
        _bench_ids = {r[0] for r in cur}
        if _bench_ids:
            active_ids = [s for s in active_ids if s not in _bench_ids]
            if not active_ids:
                return []
```
(the existing `cur = conn.cursor()` two lines below can stay — it re-fetches the same cursor object on the fakes and a fresh one on psycopg2; both fine.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_activation_assigner.py tests/execution/test_find_negative_benchmark_exclusion.py tests/execution/test_activation_apply.py tests/strategies/test_auto_demote_registry_sync.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/backtest/activation_assigner.py src/execution/strategy_weights.py tests/backtest/test_activation_assigner.py tests/execution/test_find_negative_benchmark_exclusion.py && git commit -q -m "feat(activation): benchmark sleeves always eligible + never auto-demoted (amendment 1 D-D1/D-D2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main
```

---

### Task 6: Rollout runbook + records (orchestrator-executed; operator-timed steps flagged)

**Files:**
- Modify: `docs/archive/changelog.md` (new first bullet under `## Recent Changes`), `/root/CLAUDE.md` (Current Projects bullet), memory `project_benchmark_relative_sizing_20260829.md`, `.superpowers/sdd/2026-08-29-benchmark-relative-sizing/progress.md`
- Runtime: `.env`, `pipeline_config`, transient units

**Interfaces:** none (procedure). Every step verifies its own effect; a step that cannot be verified is reported as NOT done.

- [ ] **Step 1 (D-E1, FIRST): pause the apply flag**

```bash
cd /root/openclaw && sed -i 's/^OPENCLAW_BENCH_RELATIVE_SIZING=1/OPENCLAW_BENCH_RELATIVE_SIZING=0/' .env && grep -nE '^OPENCLAW_BENCH_RELATIVE_SIZING=' .env
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service && sleep 5 && node -e "require('dotenv').config({path:'/root/openclaw/.env'}); console.log('BENCH_RELATIVE_SIZING=' + process.env.OPENCLAW_BENCH_RELATIVE_SIZING + ' EXIT_HOOK_LIVE=' + process.env.OPENCLAW_EXIT_HOOK_LIVE)" && curl -s -m 5 http://127.0.0.1:3000/api/dashboard-build
```
Expected: `=0` (shadow; `bench_relative_sizing_enabled()` is `== '1'`), `EXIT_HOOK_LIVE=1`, dashboard build responds.

- [ ] **Step 2: verify migration 151**

Migration 151 is applied by the Step 1 restart (postgres.js replays `src/database/migrations/*.sql` on start; 151 is idempotent). Verify with psycopg2:

```bash
cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" python3 -c "
import sys; sys.path.insert(0,'src')
from execution.strategy_weights import _db
c=_db().cursor()
c.execute(\"select key, value from pipeline_config where key in ('benchmark_horizon_days','benchmark_regime_sharpe')\"); print(c.fetchall())"
```
Expected: `benchmark_horizon_days|1`; the `benchmark_regime_sharpe` row still shows the schema-1 payload (it is replaced by the sizer's next run — verified in Step 5).

- [ ] **Step 3: sleeve re-backtest (transient unit; outside 13:00–20:15 UTC on weekdays)**

```bash
cd /root/openclaw && systemd-run --unit=beta-sleeve-rebt --nice=19 -p MemoryMax=3500M -p OOMScoreAdjust=1000 -p RuntimeMaxSec=3600 -p WorkingDirectory=/root/openclaw -E PYTHONPATH=/root/openclaw/src -E PYTHONUNBUFFERED=1 -E OMP_NUM_THREADS=1 -E OPENBLAS_NUM_THREADS=1 -E NUMEXPR_MAX_THREADS=1 -E POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" /usr/bin/python3 -m backtest.unified_backtest --strategy-id S_beta_spy
```
Watch: `journalctl -u beta-sleeve-rebt -f | grep -E 'backtest_tickers|wrote run_id|hook_exits|Consumed'`. Expected: `prices: manifest backtest_tickers=['SPY']`, peak memory well under 3.5 GB, `wrote run_id=…`, `config_json.exit_hook: true`, `hook_exits` ≈ 120–130 (one per regime flip). Then:

```bash
cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" python3 -c "
import sys; sys.path.insert(0,'src')
from execution.strategy_weights import _db
c=_db().cursor()
c.execute(\"select run_id, run_at, total_sharpe, total_trades, config_json->>'exit_hook', config_json->>'hook_exits' from strategy_backtest_runs where strategy_id='S_beta_spy' and primary_window order by run_at desc limit 1\"); print(c.fetchone())
c.execute(\"select regime_state, round(sharpe::numeric,2), trade_count, round(benchmark_sharpe::numeric,2) from strategy_backtest_regimes x join strategy_backtest_runs y using(run_id) where y.strategy_id='S_beta_spy' and y.primary_window order by 1\"); print(c.fetchall())"
```
Expected sleeves ≈ LOW_VOL 0.78 / TRANSITIONING 0.40 / HIGH_VOL 0.48 / CRISIS 1.5 (D-B5) and `benchmark_sharpe` ≈ 0.80 / 0.41 / 0.49 / 1.54. Record the actual numbers in the changelog bullet. If the post-commit panel hook OOMs (known), rebuild the sleeve's panel rows separately: `systemd-run --unit=beta-panel-rebuild2 --nice=19 -p MemoryMax=3500M -p OOMScoreAdjust=1000 -p WorkingDirectory=/root/openclaw -E PYTHONPATH=/root/openclaw/src /usr/bin/python3 -m backtest.backtest_panel --rebuild --strategy-id S_beta_spy`.

- [ ] **Step 4: activation + weights**

```bash
cd /root/openclaw && export POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" PYTHONPATH=src && nice -n 19 python3 -m backtest.activation_assigner --strategy-id S_beta_spy --trigger=manual 2>&1 | grep -E 'always_on|S_beta_spy' && nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=amendment1_beta_sleeve --verbose 2>&1 | grep -E 'persisted|S_beta_spy' | tail -6 && python3 -c "
import sys; sys.path.insert(0,'src')
from execution.strategy_weights import _db
c=_db().cursor()
c.execute(\"select regime_state, eligible from strategy_regime_params where strategy_id='S_beta_spy' order by 1\"); print('eligible:', c.fetchall())
c.execute(\"select regime_state, round(daily_weight::numeric,3) from strategy_weights_by_regime where strategy_id='S_beta_spy' and is_current order by 1\"); print('weights:', c.fetchall())
c.execute(\"select reason from strategy_regime_param_changes where strategy_id='S_beta_spy' order by id desc limit 1\"); print(c.fetchone())"
```
Expected: `eligible` True ×4; four weight rows (≈ the sleeve Sharpes); latest reason ends `rule=benchmark_sleeve_always_on`.

- [ ] **Step 5: first shadow cycle verification (next 15:00 ET signals+trade run, or an intraday cycle)**

In #botjohn-log / johnbot journal: `bench_sizing.shadow[<regime>]: S_m=<v> h=1 bench=['SPY'] dropped=<n>/<N> …` with `S_m` ≈ the F5 H=1 value for the current regime. Then:

```bash
cd /root/openclaw && psql "$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" -Atc "select value from pipeline_config where key='benchmark_regime_sharpe'" | python3 -c "import json,sys; p=json.load(sys.stdin); print(p['schema'], p['as_of'], {r: p['by_regime'][r]['1'] for r in p['by_regime']})"
```
Expected: `2 <today> {'LOW_VOL': ~0.80, 'TRANSITIONING': ~0.41, 'HIGH_VOL': ~0.49, 'CRISIS': ~1.54}`. Also on the first flip day after deploy: `signal_pnl` close `strategy_exit:regime_exit` for SPY at 15:00 and NO `orphan_close` for SPY at 15:55 (continuation mint keeps the target).

- [ ] **Step 6 (OPERATOR-TIMED): re-flip after two shadow cycles**

Only after two `bench_sizing.shadow[…]` lines with sane drop lists: `sed -i 's/^OPENCLAW_BENCH_RELATIVE_SIZING=0/OPENCLAW_BENCH_RELATIVE_SIZING=1/' .env` + user-scope johnbot restart + the dotenv check from Step 1. Report to the operator before doing it.

- [ ] **Step 7: records**

`docs/archive/changelog.md` — new first bullet under `## Recent Changes` (fill in the measured sleeves/`S_m` from Steps 3–5):

```
- **2026-08-30: Benchmark-relative sizing AMENDMENT 1 landed (`<first>..<last>`; spec `docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md`).** `S_m` is now the forward, entry-tagged SPY excess Sharpe (rf 5 %, the engine's own sleeve estimator) at horizon `pipeline_config.benchmark_horizon_days` (grid 1/2/3/5/10/21, default 1 = daily cadence; cache `benchmark_regime_sharpe` schema 2): H=1 LOW_VOL <v> / TRANS <v> / HIGH_VOL <v> / CRISIS <v>. **Retraction:** the 08-29 20:36 entry's "forward-H Sharpe after LOW_VOL days is −0.14 (H=1)" was wrong — next-day is +0.80 (rf 5 %) / +1.34 (rf 0); the old `S_m`=2.01 was the *contemporaneous* (same-day VIX-tagged, rf 0) statistic and untradeable; the persisted sleeve 0.20 was arithmetically correct (true marks + rf 5 % + 21-day hold through the flip + spread). `S_beta_spy`: `exit_hook=True` → `regime_exit` on any regime change (first hook strategy live; hold stays 21), eligible in ALL regimes regardless of the slider (`benchmark_sleeve_always_on`), never auto-demoted; re-backtest run `<run_id>` sleeves <v>/<v>/<v>/<v>. `load_prices_panels(tickers=)` from manifest `backtest_tickers` (`['SPY']`) makes the sleeve fleet-backtestable. Flag `OPENCLAW_BENCH_RELATIVE_SIZING` set back to 0 (shadow) for the rollout (D-E1); re-flip after two shadow cycles. Migration 151.
```

`/root/CLAUDE.md` — replace the "Status 08-29 20:36 UTC: …" sentence in the Benchmark-relative sizing bullet with: "Status <date>: AMENDMENT 1 LANDED (`<range>`; spec `docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md`) — `S_m` forward H=1 rf 5 % (`benchmark_horizon_days`), `S_beta_spy` regime-exit hook + always-on eligibility, flag back to SHADOW (0) pending two clean `bench_sizing.shadow` cycles, then re-flip (operator)."

Memory `project_benchmark_relative_sizing_20260829.md`: append a "STATUS <date>" line with the same facts + actual numbers; update the MEMORY.md index line. SDD ledger: append the task-by-task record.

- [ ] **Step 8: Commit records**

```bash
cd /root/openclaw && git add docs/archive/changelog.md && git commit -q -m "docs: amendment 1 rollout record (S_m forward H=1, regime-exit sleeve, always-on activation, shadow re-arm)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT" && git push -q origin main && git status --short | grep -vE 'manifest|registry|S_g3m|S_lead_lag|signatures' | head
```
Expected: last line empty (no campaign files left uncommitted).
