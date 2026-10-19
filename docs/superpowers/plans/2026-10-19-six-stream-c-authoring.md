# Stream C: Strategy-Authoring Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give LLM-authored strategies a shared, tested vocabulary: mechanical look-ahead and hygiene lint rules, `safe_ops` and `indicators`, episode and crossing windows, a session resampler, an event-study helper, and a sympy-validated `SIGNAL = "..."` expression DSL piloted on three new `<orig>_dsl` candidates.

**Architecture:** Every new module lives under `src/strategies/` (already an allowed lint root) except `src/research/event_study.py`. Nothing existing migrates. The one edit to existing behaviour, `compute_stops_and_targets` routing through `indicators.atr(method='close_diff')`, is byte-identical and tested against an inline copy of the old formula. The DSL is opt-in per class. A class that declares `SIGNAL` gets a concrete `generate_signals` installed by `BaseStrategy.__init_subclass__`. `BaseStrategy.generate_signals` stays abstract, so adapters such as `OxfordBaseStrategy` and `CohortBaseStrategy` remain abstract.

**Tech Stack:** Python 3.13, pandas 3.0.2, numpy, sympy 1.14 (installed; parse only, never `lambdify` or numba), DuckDB (already used by `src/data/parquet_store.py`), pytest.

**Spec:** `docs/specs/2026-10-19-six-repo-adoptions-spec.md` §3 (C1–C7), §0, §6, §7. Fit-review notes: `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/NOTES-{tdengine,pypde,diffrax,questdb}.md`.

Worktree: `/root/openclaw/.claude/worktrees/six-c` (branch `worktree-six-c`, base `4c4f921c`). Every path below is relative to it. Every test command is run exactly as written, from any cwd:
`nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/<test path> -q -W error`
(the worktree `pytest.ini` sets `pythonpath = src`).

## Global Constraints

Spec §0, verbatim:
- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history. A one-off re-layout that preserves every row byte-for-byte (A1) is a re-write of the container, not of the data, and is allowed under R1.
- Backtest side is AUTHORITATIVE (2026-08-07 ruling). Anything that changes a backtest input (D1) lands flag-gated, default off, and flips only with a fleet epoch.
- Every new behaviour that can change sizing, signals or gate outcomes ships behind an `OPENCLAW_*` env flag, default = pre-change behaviour, byte-identical when unset. Pure bug fixes with no gated consumer (B2 OU estimator, B3 MC seed/bridge, A2 windowed reads, A4 doc/dormant) need no flag; the plan says which.
- Never read the whole `prices.parquet` or `options_eod.parquet` in a test or an implementer session. Tests use synthetic frames; parquet tests write a tiny file to `tmp_path`.
- Tests reach the REAL Postgres when `.env` is importable — stub every DB gate in fixtures (`monkeypatch` the connect function); fixture tickers must not be real symbols (`ZZTEST1`, not `AAA`).
- The box is 2-core / 8 GB / no swap and shared with the live pipeline. Implementers run single-file pytest only, `nice -n 19`, never the full suite, never while a fleet unit is active.
- `strategy_lint.ALLOWED_ROOTS` stays as is; new shared modules live under `src/strategies/` (already an allowed root) or `src/lib/`.
- Observability: every new gate/shadow emits one log line per cycle with a fixed prefix; a missing line means the process died, never "nothing to report". Never silent.
- Commit per task; edits, `git commit` and `git push` as separate tool calls (ERR-20261018-001).

Stream C flags. This stream adds **no `OPENCLAW_*` flag**. Per §0, each item and the reason it is flag-free:
- C1: the new rules are WARN-only by default. `ok`, `violations` and the exit code of the existing lint are unchanged without `--strict`, and nothing in the pipeline passes `--strict`.
- C2–C6: new modules with no existing caller. No strategy, gate or sizer is migrated.
- C3: the `compute_stops_and_targets` edit is byte-identical, proven by Task 3's oracle test. `engine.py` HV20 and `backtest_panel.py` hv21 get comments only.
- C7: only classes that declare `SIGNAL` change behaviour. No existing class declares it. The three pilots are new candidate ids that reach live only through the normal research promotion path (R4).

Never call `generate_signatures.main()`: it rewrites the real `strategy_signatures.json`. Never hand-edit `src/strategies/manifest.json`: it is live and rewritten on main. Candidate registration is the deferred, controller-run script from Task 10.

## Decisions beyond the spec (read before Tasks 4, 8, 9, 10)

These are pinned because the spec text cannot be implemented as written, or leaves a choice open that executors would otherwise resolve differently. Items 1–3 and 9 are also reported as spec defects.
1. **DSL representation.** `compile_signal(...)` returns a `CompiledSignal` whose `__call__(panel) -> pd.DataFrame`, not `pd.Series`. The input panel is a DataFrame with two-level columns `(field, ticker)`, built by `make_panel({'close': wide, 'volume': wide})`. The output is a wide `date × ticker` frame, and `generate_signals` reads `.iloc[-1]`, a Series over tickers. A `Series` return cannot carry Amihud's cross-sectional `rank_pct` over a close panel plus a volume panel.
2. **Declared columns.** A new class attribute `SIGNAL_COLUMNS: frozenset = frozenset({'close'})`. The spec's `compile_signal(columns=...)` has no source on the class.
3. **Two-sided rules.** `SIGNAL_DIRECTION` accepts `'long' | 'short' | 'both'`. For `'both'`, `SIGNAL_THRESHOLD` is a tuple `(lo, hi)`. Amihud and Bollinger are two-sided, and the spec's `'long'|'short'` cannot express them.
4. **Threshold comparisons are strict.** `'long'`: `v > T`. `'short'`: `v < T`. `'both'`: LONG `v > hi`, SHORT `v < lo`. A pilot whose original rule is inclusive (Amihud `>= 0.80`) overrides `select_signals` (Task 9 hook).
5. **Extra DSL vocabulary.** `abs(x)` is allowed as arithmetic (sympy `Abs`). Amihud's ILLIQ needs it. The list of whitelisted functions is otherwise exactly the spec's.
6. **Op semantics.**
   - `std` and `zscore` use ddof=1 (pandas default, matching `indicators.zscore`). The Bollinger pilot folds ddof=0 into its threshold (Task 10).
   - `max(x, n)` and `min(x, n)` are rolling only; there is no elementwise form.
   - Division compiles to `safe_div(a, b, fill=np.nan)`, so it returns NaN, never 0.
   - NaN cells are "no bar": every windowed op runs on each column's own `dropna()` series and is reindexed back.
   - The compiler never tail-slices by `required_lookback`, because that would change the ema and rsi results.
   - Boolean and comparison syntax (`<`, `&`, `and`) is rejected in this campaign.
7. **`episodes_to_trades` takes `direction='LONG'`** (a string or a Series aligned with `price`). Short trades in the czar loop need it. This is an addition to the spec signature and stays backward compatible.
8. **Registration follows the X1 / gk63 precedent.** Each pilot gets an `_IMPL_MAP` entry and a `.requirements.json` sidecar on the branch. The manifest `candidate` entry is inserted by `scripts/register_dsl_pilots.py --apply`, run by the controller after merge (§6.5). The id equals the file stem: `S_golden_cross_trend_signal_tv2_dsl`, `S_amihud_illiquidity_premium_dsl`, `oxf_bollinger_momentum_dsl`. The golden-cross original's class id (`S_golden_cross_trend_signal`) differs from its file stem `_tv2`, so the pilot id is taken from the stem.
9. **`unguarded_parquet_read` names `strategies.asof`, a D1 module that merges after C** (§6.3). The rule is pure AST: it matches the import statement and never imports the module. Until D lands, every reader WARNs and the StrategyCoder prompt does not tell authors to import it.

## Review Focus

1. **A hostile or broken `SIGNAL` string.** Example: `"close.__class__"` or `"__import__('os')"`. The string must be rejected by the Python-`ast` pass before sympy's `eval` runs, both in the lint process and at class definition. Test: Task 8 `test_hostile_strings_never_reach_sympy`.
2. **WARN-only findings on a real fleet file.** They must leave `ok`, `violations`, the exit code and `TestFleetIsClean` unchanged, because the orchestrator rejects on `ok === false`. Test: Task 1 `test_warn_rules_never_change_ok_or_rc`.
3. **A panel whose last row is a union-calendar weekend row** (every equity NaN). The DSL default must emit zero signals and still print its `[signal_dsl]` line, never act on stale values. Test: Task 9 `test_weekend_last_row_emits_nothing_but_logs`.
4. **A resample bucket whose last calendar session is after the data end** (an in-progress month), or whose only post-session row is a Saturday crypto row. The label must never be a future date, and the Saturday value must not leak into the Friday bar. Test: Task 6 `test_partial_bucket_label_never_in_future_and_no_weekend_leak`.
5. **Duplicate events and unpriced tickers in the event study.** Duplicates are the same ticker on the same anchor session twice; unpriced tickers are absent from the panel. Each (ticker, anchor) pair is counted once, unpriced events are excluded, and `n` reports only priced observations. Test: Task 7 `test_duplicates_and_unpriced_events`.

---

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/strategies/strategy_lint.py` (modify) | WARN channel (4 rules), `--strict`, `--census`; later the `signal_expr` error kind | 1, 8 |
| `src/strategies/validate_strategy.py:138-143` (modify) | print lint warnings to stderr; never fail on them | 1 |
| `src/strategies/safe_ops.py` (create) | `EPS`, `safe_div`, `safe_log`, `clip_pos` | 2 |
| `src/strategies/indicators.py` (create) | `atr`, `realized_vol`, `zscore`, `mavg`, `sma`, `ema`, `csum`, `diff`, `rsi` | 3 |
| `src/strategies/base.py` (modify) | `ind` alias + `compute_stops_and_targets` via `ind.atr` (Task 3); SIGNAL DSL integration (Task 9) | 3, 9 |
| `src/execution/engine.py:1127`, `src/backtest/backtest_panel.py:278` (comment only) | name `indicators.realized_vol` | 3 |
| `src/strategies/windows.py` (create) | `state_window`, `statecount`, `stateduration`, `event_window`, `session_gaps`, `episodes_to_trades`, `crossing` | 4, 5 |
| `src/strategies/resample.py` (create) | `session_bars`; re-exports `next_session`, `prev_session` | 6 |
| `src/research/event_study.py` (create) | `markout` + CLI | 7 |
| `src/strategies/signal_expr.py` (create) | validator, compiler, `formula_hash`, `make_panel` | 8 |
| `src/strategies/implementations/{S_golden_cross_trend_signal_tv2_dsl,S_amihud_illiquidity_premium_dsl,oxf_bollinger_momentum_dsl}.py` + `.requirements.json` (create) | pilots | 10 |
| `src/strategies/registry.py` (modify, `_IMPL_MAP`) · `src/strategies/generate_signatures.py` (modify) · `scripts/register_dsl_pilots.py` (create) · `src/agent/prompts/subagents/strategycoder.md` (modify) | registration, signatures, prompt | 10 |

---

### Task 1: C1 — mechanical lint rules on a separate WARN channel

**Files:**
- Modify: `src/strategies/strategy_lint.py` (add after `lint_source`, `:164-244`; replace `main`, `:260-270`)
- Modify: `src/strategies/validate_strategy.py:138-143`
- Test: `tests/strategies/test_strategy_lint.py` (edit the assertion at `:240`; add class `TestWarnRules`)

**Interfaces:**
- Produces:
  - `WARN_KINDS: tuple[str, ...] = ('negative_shift', 'unguarded_parquet_read', 'rolling_without_min_periods', 'raw_epsilon_literal')`
  - `lint_warnings(source: str, filename: str = '<candidate>') -> list[Violation]`
  - `lint_file_warnings(path) -> list[Violation]`
  - `format_warnings(violations) -> list[str]` (each line is `'[strategy-lint-warn] line {n}: {kind}: {detail}'`)
  - `census(paths: list[str]) -> dict`
  - `main(argv)` accepting `--strict` and `--census`
- `lint_source` and `lint_file` are unchanged: they never return WARN kinds.

Rule definitions (pure AST; the candidate is never imported):
- `negative_shift`: a negative int literal (`Constant(int < 0)`, or `UnaryOp(USub, Constant(int > 0))`) in one of two places: as the first positional arg or `periods=` keyword of a Call whose func is an Attribute named `shift`; or as any positional arg or the `n=`/`periods=` keyword of a Call whose func is a Name/Attribute named `lag`.
- `unguarded_parquet_read`: a Call whose func name/attr is `read_parquet` or `read_csv`, in a module with none of `import strategies.asof`, `from strategies.asof import …`, `from strategies import asof`.
- `rolling_without_min_periods`: a Call whose func is an Attribute named `rolling`, with no `min_periods` keyword.
- `raw_epsilon_literal`: a float `Constant` (not int, not bool) with `1e-12 <= abs(v) <= 1e-6`.
- A finding inside an `if __name__ == '__main__':` block gets ` [__main__]` appended to `detail`. Such findings are not exempt; the census counts them separately (`hits_in_main`).

- [ ] **Step 1: Write the failing tests**

In `tests/strategies/test_strategy_lint.py`, change `test_cli_json_contract_on_a_clean_file`'s expected payload to `{'ok': True, 'violations': [], 'warnings': []}`. This is a deliberate contract widening; the orchestrator reads only `.ok` and `.violations`. Then add `class TestWarnRules(unittest.TestCase)` with:
- `test_each_rule_fires`. Assert the WARN kind returned by `sl.lint_warnings(OK_HEAD + snippet)` for each snippet:
  - `'x = s.shift(-1)\n'` and `'x = s.shift(periods=-2)\n'` → `negative_shift`
  - `'x = lag(s, -3)\n'` → `negative_shift`
  - `'import pandas as pd\nd = pd.read_parquet("p")\n'` → `unguarded_parquet_read`
  - `'r = s.rolling(20).mean()\n'` → `rolling_without_min_periods`
  - `'e = 1e-9\n'` → `raw_epsilon_literal`
- `test_rules_do_not_fire_on_clean_forms`. Each of these returns `[]`:
  - `'s.shift(1)\n'`
  - `'s.rolling(20, min_periods=20).mean()\n'`
  - `'e = 1e-3\n'` and `'e = 0.0\n'`
  - `'from strategies.asof import asof_align\nimport pandas as pd\npd.read_parquet("p")\n'`
- `test_main_block_findings_are_tagged`. A `shift(-1)` under `if __name__ == '__main__':` yields `detail.endswith('[__main__]')`.
- `test_warn_rules_never_change_ok_or_rc` (Review Focus 2). Write a temp file with `s.rolling(5).mean()` and `s.shift(-1)`, then call `sl.main([path])`. Assert `rc == 0`, `payload['ok'] is True`, `payload['violations'] == []`, `len(payload['warnings']) == 2`, and that every warnings row has keys exactly `{'file','line','kind','detail'}`. Also assert `sl.lint_file(path) == []`.
- `test_strict_promotes_warnings`. `sl.main(['--strict', path])` gives `rc == 1`, `ok is False`, two `violations`, and `warnings == []`.
- `test_census_shape`. `sl.census([path_a, path_b])` returns `{'files': 2, 'rules': {kind: {'files': int, 'hits': int, 'hits_in_main': int} for kind in WARN_KINDS}}`.
- `test_census_cli_exits_zero`. `sl.main(['--census', path])` gives rc 0 and JSON with key `rules`.
- `test_validate_prints_warnings_to_stderr`. Monkeypatch `strategies.strategy_lint.lint_file_warnings` to return one WARN Violation and `lint_file` to return one `import` Violation. Then `validate_strategy.validate(<tmp .py file>)` returns early with `ok is False` without importing the candidate, and capsys stderr contains `'[strategy-lint-warn]'`. Warnings print before the error check, so this path never reaches the import or the DB.

- [ ] **Step 2: Run the new tests and confirm they fail**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_strategy_lint.py -q -W error -k "WarnRules or clean_file"`
Expected: FAIL (`AttributeError: ... 'lint_warnings'`, and a payload mismatch).

- [ ] **Step 3: Implement the rules, `census`, the argparse `main`, and the validate stderr lines**

- `main`: parse argv with `argparse` (`paths` nargs `*`, `--strict`, `--census`).
  - With no paths and no `--census`, print the usage line and return 2, as today.
  - Single-path behaviour must stay byte-identical except for the added `warnings` key.
  - `--census` with no paths globs `implementations/*.py` next to this file, prints the JSON, and returns 0.
- `validate_strategy.py`: immediately BEFORE the existing `if lint_violations:` block, print each `format_warnings(lint_file_warnings(abs_path))` line to `sys.stderr`. Warnings never change the return value.

- [ ] **Step 4: Run the whole lint test file (includes `TestFleetIsClean`)**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_strategy_lint.py -q -W error`
Expected: PASS (all prior tests plus the new ones).

- [ ] **Step 5: Run the two Node gate tests that spawn the real lint**

Run: `nice -n 19 node --test /root/openclaw/.claude/worktrees/six-c/tests/agent/test_zero_signal_gate.test.js /root/openclaw/.claude/worktrees/six-c/tests/agent/test_ic_screen_gate.test.js`
Expected: PASS, unchanged.

- [ ] **Step 6: Commit**

```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/strategy_lint.py src/strategies/validate_strategy.py tests/strategies/test_strategy_lint.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(lint): C1 WARN rules negative_shift/unguarded_parquet_read/rolling_without_min_periods/raw_epsilon_literal + --strict/--census"
```

---

### Task 2: C3a — `safe_ops`

**Files:**
- Create: `src/strategies/safe_ops.py`
- Test: `tests/strategies/test_safe_ops.py`

**Interfaces:**
- Produces:
  - `EPS: float = 1e-9`
  - `safe_div(a, b, fill=0.0)`: returns `a / b` where `abs(b) >= EPS`, `fill` where `abs(b) < EPS`, and NaN where `b` is NaN (NaN is never filled).
  - `safe_log(x)`: `log(x)` where `x > EPS`, else NaN.
  - `clip_pos(x)`: `maximum(x, EPS)`, preserving NaN.
- All three accept a scalar, `np.ndarray`, `pd.Series` or `pd.DataFrame` and return the same kind, keeping index and columns. None emits a numpy RuntimeWarning (use `np.errstate`).

- [ ] **Step 1: Write the failing tests**
- `test_safe_div_fills_tiny_and_zero_denominators_both_signs`. `safe_div(pd.Series([1.,1.,1.,1.]), pd.Series([0., -1e-12, 1e-12, 2.]))` equals `[0., 0., 0., 0.5]`, and with `fill=np.nan` equals `[nan, nan, nan, 0.5]`.
- `test_safe_div_nan_denominator_stays_nan`.
- `test_safe_div_dataframe_keeps_labels`. A DataFrame over `ZZTEST1`/`ZZTEST2` keeps its index and columns.
- `test_safe_div_scalar`. `safe_div(1.0, 0.0) == 0.0`.
- `test_safe_log`. `safe_log(pd.Series([np.e, 0., -1., 1e-10]))` equals `[1., nan, nan, nan]`.
- `test_clip_pos`. `clip_pos(pd.Series([-1., 0., 2., np.nan]))` equals `[1e-9, 1e-9, 2., nan]`.
- `test_no_runtime_warnings`. Call all three on zeros and negatives under `warnings.simplefilter('error')`.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_safe_ops.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.safe_ops`).

- [ ] **Step 3: Implement `src/strategies/safe_ops.py`**

The module docstring states the single epsilon policy and that `strategy_lint`'s `raw_epsilon_literal` rule points authors here.

- [ ] **Step 4: Run and confirm pass**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_safe_ops.py -q -W error`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/safe_ops.py tests/strategies/test_safe_ops.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C3 safe_ops (EPS=1e-9, safe_div, safe_log, clip_pos)"
```

---

### Task 3: C3b — `indicators` + `BaseStrategy.ind` + byte-identical stops

**Files:**
- Create: `src/strategies/indicators.py`
- Modify: `src/strategies/base.py:141-340` (class attribute `ind`; `compute_stops_and_targets` lines `:300-303`)
- Modify (comment only): `src/execution/engine.py:1127-1133`, `src/backtest/backtest_panel.py:278-279`
- Test: `tests/strategies/test_indicators.py`

**Interfaces:**
- Consumes: `safe_ops.safe_div` (Task 2).
- Produces: every function takes a `pd.Series`, returns a `pd.Series` on the same index, and never raises on empty or short input (it returns all-NaN instead).
  - `atr(bars_or_close, n=14, *, method='true_range', fallback_pct=0.02)`
    - `method='true_range'` needs a DataFrame with `high`/`low`/`close`: TR = max(h−l, |h−pc|, |l−pc|), same as `oxford_crabel.true_range_series`.
    - `method='close_diff'` takes a Series: `s.diff().abs()`.
    - Either way the result is `.rolling(n).mean()`. Where the result is NaN or ≤ 0 and `fallback_pct` is not None, it becomes `close * fallback_pct`.
    - Any other `method` raises ValueError.
  - `realized_vol(close, n=20, *, log=True, annualize=252)`
    - returns are `log(close).diff()` when `log`, else `close / close.shift(1) - 1`
    - result is `.rolling(n).std() * sqrt(annualize)`; `annualize` falsy means no scaling
  - `zscore(s, n, *, min_periods=None)`: `(s − rolling mean) / rolling std`, ddof=1, `min_periods` defaulting to `n`, division via `safe_div(fill=np.nan)`.
  - `mavg(s, n)` and `sma(s, n)`: `s.rolling(n).mean()`.
  - `ema(s, n)`: `s.ewm(span=n, adjust=False).mean()`.
  - `csum(s)`: `s.cumsum()`.
  - `diff(s, *, ignore_negative=False)`: `s.diff()`; with `ignore_negative`, negative values become NaN.
  - `rsi(close, n=14)`: Wilder (`ewm(alpha=1/n, adjust=False)` on gains and losses); 100 where losses ≤ `EPS`.
- `BaseStrategy.ind = indicators`, a class attribute (module alias).

- [ ] **Step 1: Write the failing tests**
- `test_atr_close_diff_equals_old_base_formula`.
- `test_atr_true_range_matches_oxford`. `atr(bars, 20).iloc[-1] == oxford_crabel.atr(bars, 20)` on a synthetic OHLC frame.
- `test_atr_fallback_and_bad_method`.
- `test_realized_vol_reproduces_engine_hv20`. `realized_vol(c, 20, log=False)` equals `c.pct_change().rolling(20).std() * 252 ** 0.5`.
- `test_realized_vol_reproduces_backtest_hv21`. `realized_vol(c, 21, log=True)` equals `np.log(c).diff().rolling(21).std() * math.sqrt(252)`.
- `test_zscore_min_periods_and_zero_std_is_nan`.
- `test_ema_matches_oxford`. `ema(s, 12).iloc[-1] == oxford_crabel.ema(s, 12)`.
- `test_rsi_matches_oxford_wilder`. `abs(rsi(s, 14).iloc[-1] - oxford_crabel.rsi_wilder(s, 14)) < 1e-12`, plus a monotone-up series giving 100.
- `test_diff_ignore_negative`, `test_csum_mavg_sma`.
- `test_short_and_empty_inputs_return_nan_series`. Every function on `pd.Series([], dtype=float)` and on a 3-point series returns the same index and raises nothing.
- `test_base_exposes_ind`. `BaseStrategy.ind is indicators`, and an instance's `.ind.sma` exists.
- `test_compute_stops_and_targets_byte_identical`. 300 seeded random cases:
  - lengths drawn from {0, 1, 13, 14, 15, 40}, plus constant series and series with an interior NaN
  - `current_price != series.iloc[-1]`
  - both directions, both `OPENCLAW_TARGET_MODE` values (via monkeypatch), and regimes from `REGIME_ATR_SCALE`
  - the oracle is an inline copy of the pre-change body of `base.py:300-339` (copied into the test, not imported)
  - assert dict equality

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_indicators.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.indicators`).

- [ ] **Step 3: Implement `indicators.py`; wire `ind` and the stops**

In `compute_stops_and_targets`, replace the two lines that compute `diff` and `_atr_raw` with:
`_atr_raw = self.ind.atr(prices_series, 14, method='close_diff', fallback_pct=None).iloc[-1] if len(prices_series) >= 14 else float('nan')`.
Keep the existing `current_price * 0.02` fallback line unchanged.

Add one comment at `engine.py:1127` and one at `backtest_panel.py:278`: `# Same quantity as strategies.indicators.realized_vol(close, 20, log=False) / (21, log=True); not migrated (re-gate risk, spec §3 C3).`

- [ ] **Step 4: Run the new file and the existing stops file**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_indicators.py -q -W error`
Then: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_base_stops_targets.py -q -W error`
Expected: both PASS.

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/indicators.py src/strategies/base.py src/execution/engine.py src/backtest/backtest_panel.py tests/strategies/test_indicators.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C3 indicators + BaseStrategy.ind; stops via ind.atr(close_diff), byte-identical"
```

---

### Task 4: C2 — `windows.py` episode primitives + loop-parity tests

**Files:**
- Create: `src/strategies/windows.py`
- Test: `tests/strategies/test_windows.py`

**Interfaces:**
- Produces. All functions are causal (the value at bar i uses bars ≤ i), except that `close_unfinished=False` needs the final bar to know an episode is unfinished. Each returns a Series on the input index.
  - `state_window(key: pd.Series, *, nan_breaks=True) -> pd.Series[int]`
    - Every maximal run of equal consecutive non-NaN keys gets a new id, counting from 1; NaN rows get 0.
    - `nan_breaks=True`: `A, NaN, A` → `1, 0, 2`. `False`: → `1, 0, 1`.
  - `statecount(cond: pd.Series[bool]) -> pd.Series[int]`: consecutive True bars ending at this bar; 0 where False.
  - `stateduration(cond) -> pd.Series[int]`: calendar days from the current True run's first bar to this bar; −1 where False. Needs a DatetimeIndex.
  - `event_window(start, end, *, close_unfinished=False, same_bar_single=False, start_true_for=0, end_true_for=0) -> pd.Series[int]`
    - Episode id, 0 = flat. An id marks bars where the position is held after that bar's decision: the entry bar carries the id, the end bar carries 0.
    - A repeated start while open continues the episode; an end while flat is a no-op.
    - A bar where both are true: by default start wins (a new episode begins on that bar, closing any open one). With `same_bar_single=True`, end wins (the bar is flat).
    - `start_true_for=n` (n ≥ 2) replaces `start` with `statecount(start) >= n`; likewise for `end`.
    - `close_unfinished=False` zeroes the ids of an episode still open on the last bar.
    - `start`/`end` must be bool dtype; NaN or non-bool raises ValueError.
    - Vectorised algorithm: `state = where(start, 1, where(end, 0, NaN)).ffill().fillna(0)`, a new episode where `state == 1 & (prev_state == 0 | (start & end))`, then `id = cumsum(new) * state`.
  - `session_gaps(index: pd.DatetimeIndex, tol) -> pd.Series[int]`: a group id starting at 1, incremented where the gap to the previous timestamp is greater than `pd.Timedelta(tol)`.
  - `episodes_to_trades(episode_ids, price, dates, *, regime=None, direction='LONG') -> list[dict]`
    - For each id k > 0: entry index i0 = first bar with k; exit index = (last bar with k) + 1, or the last bar of the data if k runs to the end.
    - `pnl` = `(exit-entry)/entry` for LONG, `(entry-exit)/entry` for SHORT.
    - `direction` and `regime` are a scalar or a Series positionally aligned with `price`, read at i0.
    - Row keys: `signal_date` (YYYY-MM-DD of `dates[i0]`), `exit_date`, `direction`, `entry_price`, `exit_price`, `pnl`, `regime_state` (None when `regime` is None).

- [ ] **Step 1: Write the failing tests**

Unit tests:
- `test_state_window_nan_breaks_both_modes`
- `test_statecount_and_stateduration`. Include a weekend gap so the duration is calendar days.
- `test_event_window_basic_collapse_and_noop_end`
- `test_event_window_same_bar_start_and_end_both_modes`. Cover the flat case and the open case.
- `test_event_window_true_for_hysteresis`. `end_true_for=2` ignores a 1-bar end.
- `test_event_window_close_unfinished_both_modes`
- `test_event_window_rejects_nan_and_non_bool` (`pytest.raises(ValueError)`)
- `test_session_gaps`
- `test_episodes_to_trades_long_short_and_final_bar_close`

Loop-parity fixtures. Copy each loop body into the test as a plain function; never import the strategy module.
- `test_parity_golden_cross_loop`
  - Fixture: `_golden_loop(s, fast, slow, regime_map)` copies `S_golden_cross_trend_signal_tv2.py:195-224`, using fast=5, slow=20.
  - Synthetic series: deterministic sine plus trend, length 300.
  - Helper path: `start = (f > sl)`, `end = (f <= sl)` (NaN compares give False), both forced False for positions `< slow`; `event_window(start, end)`; `episodes_to_trades(..., regime=pd.Series(dates).map(regime_map).fillna('LOW_VOL').values)`.
  - Assert the lists of `(signal_date, regime_state, pnl)` are equal, with pnl to 1e-12.
  - Assert the quirks are exercised (choose the series phase so these hold): the unmasked `start` is True at position `slow-1`, so entry lands on position `slow`, not `slow-1`; the raw state is bullish on the last bar, so the unfinished episode is dropped; and there are at least 3 trades.
- `test_parity_czar_loop`
  - Fixture: `_czar_loop(prices_s, dir_at, min_lb, regime_s, step=5)` copies `S_czar_loss_btc_directional.py:195-206`. `_fit_and_predict` is replaced by a precomputed per-position `dir_at` ('LONG' / 'SHORT' / None), and the regime asof lookup by `regime_s`.
  - Helper path: sample positions `range(min_lb, len, 5)`; `key` = `dir_at` at those positions (None → NaN); append the true final bar with the last sampled key (the czar force-close uses `prices_s.iloc[-1]`); `state_window(key)`; `episodes_to_trades(..., direction=key, regime=...)`.
  - Assert the lists of `(signal_date, regime_state, pnl)` are equal.
  - Assert the quirks are exercised: `(len(prices_s) - 1 - min_lb) % 5 != 0`; `dir_at` contains a same-bar LONG→SHORT flip on a sampled position, a flat gap, and a run still open at the end.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_windows.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.windows`).

- [ ] **Step 3: Implement `src/strategies/windows.py`**

The module docstring cites TDengine `STATE_WINDOW` / `EVENT_WINDOW` / `STATECOUNT` / `SESSION` and the do-not-copy notes. It states two rules explicitly: NaN never glues windows, and the default `same_bar_single=False` differs from TDengine's one-row window.

- [ ] **Step 4: Run and confirm pass**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_windows.py -q -W error`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/windows.py tests/strategies/test_windows.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C2 windows (state/event windows, statecount, session_gaps, episodes_to_trades) + golden/czar loop parity"
```

---

### Task 5: C4 — `windows.crossing`

**Files:**
- Modify: `src/strategies/windows.py` (append)
- Test: `tests/strategies/test_windows.py` (append)

**Interfaces:**
- Produces: `crossing(a: pd.Series, b: pd.Series | float, *, direction='up') -> pd.Series[bool]`
  - `'up'`: `(a.shift(1) <= b_prev) & (a > b)`
  - `'down'`: `(a.shift(1) >= b_prev) & (a < b)`
  - `'any'`: either
  - `b` may be a scalar. Any NaN in the current or previous pair gives False. The first bar is False. Any other `direction` raises ValueError.
- The two `should_exit` hooks (`S_coint_pairs_sector_v2.py:397`, `S_beta_spy.py:103`) are NOT edited.

- [ ] **Step 1: Write the failing tests**
- `test_crossing_up_down_any`. Hand-built a/b with a touch-then-cross (`prev == b` counts as up) and a touch-without-cross (`now == b` does not).
- `test_crossing_scalar_b_and_nan`
- `test_crossing_matches_kalman_idiom`. On a random pair, `crossing(k, l, direction='up')` equals `(k.shift(1) <= l.shift(1)) & (k > l)`, the idiom at `S_kalman_laguerre_trend_crossover_tv2.py:158`, with NaN → False.
- `test_crossing_bad_direction`

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_windows.py -q -W error -k crossing`
Expected: FAIL (`ImportError: cannot import name 'crossing'`).

- [ ] **Step 3: Implement `crossing`**
- [ ] **Step 4: Run the whole file and confirm pass**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_windows.py -q -W error`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/windows.py tests/strategies/test_windows.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C4 windows.crossing (diffrax Event.direction semantics)"
```

---

### Task 6: C5 — session resampler

**Files:**
- Create: `src/strategies/resample.py`
- Test: `tests/strategies/test_resample.py`

**Interfaces:**
- Consumes: `lib.trading_calendar.sessions`, `next_session`, `prev_session` (`src/lib/trading_calendar.py:144-170`).
- Produces: `session_bars(wide: pd.DataFrame, rule: str, *, label='last_session', fill='none') -> pd.DataFrame`.
  - `rule` ∈ {`'W'`, `'M'`, `'Q'`}. Buckets are pandas periods `W-SUN`, `M` and `Q-DEC` of the row dates.
  - Per bucket, `label` = the last NYSE session in the bucket that is ≤ `wide.index.max()`. A bucket with no such session is omitted.
  - Per column, the value is the **last non-NaN** among rows with `bucket_start ≤ date ≤ label`. Rows after the label (e.g. a Saturday crypto row) never contribute.
  - `fill='prev'` forward-fills across output rows. The docstring says to use it for state series only, never prices.
  - The output index is a DatetimeIndex of labels named `date`.
  - Unknown `rule`/`label`/`fill`, or a non-DatetimeIndex, raises ValueError.
- `from lib.trading_calendar import next_session, prev_session` is re-exported (`__all__`).
- No existing strategy is migrated.

- [ ] **Step 1: Write the failing tests**

Fixture: copy the `_write_master` pattern from `tests/lib/test_trading_calendar.py:14-25`. Sessions are business days 2026-06-01 → 2026-09-30 with 2026-07-03 and 2026-07-31 inactive. Monkeypatch `tc._alpaca_sessions` to `lambda s, e: None`, so the alpaca binary is never spawned.

Tests:
- `test_month_label_skips_month_end_holiday`. The July label is `2026-07-30`.
- `test_week_label_skips_friday_holiday`. The week of 2026-06-29 is labelled `2026-07-02`.
- `test_quarter_labels`. The Q2 label is `2026-06-30`.
- `test_values_are_last_non_nan_in_bucket`
- `test_partial_bucket_label_never_in_future_and_no_weekend_leak` (Review Focus 4)
  - Data ends Wed 2026-09-16, and a Saturday 2026-09-12 row is present for a `ZZCRYPTO1` column.
  - The September label is `2026-09-16`, not `09-30`.
  - The week-of-09-07 bar for `ZZCRYPTO1` takes Friday 09-11's value, not Saturday's.
- `test_fill_prev_vs_none`
- `test_bad_arguments_raise`
- `test_reexports`. `resample.next_session is trading_calendar.next_session`.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_resample.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.resample`).

- [ ] **Step 3: Implement `src/strategies/resample.py`**
- [ ] **Step 4: Run and confirm pass** (same command; expected PASS)
- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/resample.py tests/strategies/test_resample.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C5 session_bars resampler labelled on last NYSE session"
```

---

### Task 7: C6 — event-study markout helper + CLI

**Files:**
- Create: `src/research/event_study.py`
- Test: `tests/research/test_event_study.py`

**Interfaces:**
- Produces: `markout(events: pd.DataFrame, prices_wide: pd.DataFrame, offsets=range(-5, 21)) -> pd.DataFrame` with columns `['offset', 'mean_ret', 'median_ret', 'n']`, one row per offset in the given order. When `n == 0`, mean and median are NaN.
  - **Anchor (offset 0):** the last trading session ≤ `event_date`, found by a DuckDB `ASOF JOIN` of events onto the session table. The session axis is `trading_calendar.sessions(prices_wide.index.min(), prices_wide.index.max())`.
    - A Saturday event anchors to Friday.
    - An after-close event dated on a session anchors to that session, so +1 is the first full session after the release. The docstring says so.
  - **Return:** `ret(k) = close[s0+k] / close[s0] - 1`, a cumulative markout from the anchor close, so offset 0 ≡ 0.
  - A missing price at the anchor or the target excludes that observation. Duplicate (ticker, anchor) pairs count once. Tickers absent from `prices_wide` are excluded.
  - `event_date` may be str, date or datetime.
  - Implementation: one shared `_markout_sql(con, events_view, prices_view, sessions_view, offsets) -> pd.DataFrame`. `markout` registers a long melt of `prices_wide` (event tickers only). The connection runs `SET memory_limit='1GB'`.
- CLI: `python -m research.event_study --master {earnings,insider} --since YYYY-MM-DD [--offsets -5:20] [--events-path P] [--prices-path P] [--calendar-path P]`.
  - Defaults: `data/master/earnings.parquet` (event date column = the first present of `date`, `report_date`, `earnings_date`, found via DuckDB `DESCRIBE`, footer only), `data/master/insider.parquet` (`date` = filing date), and `parquet_store.PRICES_PATH`.
  - The prices read is a DuckDB view: `ticker IN (SELECT DISTINCT ticker FROM events) AND CAST(date AS DATE) >= since - 40 days`. Prices are never materialised in pandas, and the master is never read whole.
  - Prints JSON `{'master', 'since', 'events', 'rows': [...]}` to stdout and one stderr line `[event_study] master=… events=… offsets=…`. Exit 0.
  - Implementers never run it against real masters.

- [ ] **Step 1: Write the failing tests**

Fixture: a synthetic calendar master as in Task 6, with `_alpaca_sessions` stubbed, plus a deterministic `prices_wide` (`ZZTEST1..3`) whose closes make returns hand-computable (e.g. geometric 1 %/day for ZZTEST1).

Tests:
- `test_markout_hand_computed`. Offsets −2..3 give exact `mean_ret`, `median_ret` and `n`.
- `test_weekend_event_anchors_to_prior_session`
- `test_offsets_beyond_panel_reduce_n`
- `test_duplicates_and_unpriced_events` (Review Focus 5). The same (ZZTEST1, date) twice, plus `ZZMISSING`, gives n equal to the deduplicated priced count.
- `test_string_and_datetime_event_dates_equivalent`
- `test_cli_on_tmp_parquets`. Write tmp `earnings.parquet` (`ticker`, `report_date`), a long tmp `prices.parquet` (`ticker`, `date` as str, `close`) and the calendar. Call `event_study.main([...all paths...])` with capsys. Assert the JSON has `rows` of length 26 and that stderr starts with `[event_study]`.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/research/test_event_study.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: research.event_study`).

- [ ] **Step 3: Implement `src/research/event_study.py`**
- [ ] **Step 4: Run and confirm pass** (same command; expected PASS)
- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/research/event_study.py tests/research/test_event_study.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(research): C6 event_study.markout (DuckDB ASOF on trading calendar) + CLI"
```

---

### Task 8: C7a — expression compiler `signal_expr` + lint integration

**Files:**
- Create: `src/strategies/signal_expr.py`
- Modify: `src/strategies/strategy_lint.py` (new error kind `signal_expr` inside `lint_source`)
- Test: `tests/strategies/test_signal_expr.py`; `tests/strategies/test_strategy_lint.py` (append `TestSignalExprLint`)

**Interfaces:**
- Consumes: `indicators.*` (Task 3), `safe_ops.safe_div` (Task 2).
- Produces:
  - `class SignalExprError(ValueError)`
  - `DEFAULT_OPS: Mapping[str, OpSpec]`: exactly `sma, ema, std, zscore, rank_pct, atr, rsi, pct_change, max, min, lag`. `abs` is always allowed as arithmetic.
  - `validate_expr_syntax(expr: str, *, columns: set[str], ops=DEFAULT_OPS) -> None`: Python-`ast` pass only, never imports sympy.
  - `compile_signal(expr: str, *, columns: set[str], ops=DEFAULT_OPS) -> CompiledSignal`
  - `CompiledSignal`: callable `(panel: pd.DataFrame) -> pd.DataFrame`, with attributes `expr: str`, `columns: frozenset[str]` (used), `required_lookback: int`, `formula_hash: str` (16 hex).
  - `formula_hash(expr: str, *, columns: set[str] | None = None, ops=DEFAULT_OPS) -> str`. With `columns=None`, every Name that is not an op is treated as a column; `generate_signatures` uses this form without importing the strategy.
  - `make_panel(fields: Mapping[str, pd.DataFrame]) -> pd.DataFrame`: `pd.concat(fields, axis=1)` with column level names `['field', 'ticker']`.

Pinned behaviour:
- **Pass 1, Python `ast`, `mode='eval'`.**
  - Allowed nodes only: `Expression`, `BinOp` (`Add`, `Sub`, `Mult`, `Div`, `Pow`), `UnaryOp` (`USub`, `UAdd`), `Call` with a `Name` func and positional args only, `Name`, and an int/float `Constant` (bool excluded).
  - Everything else raises `SignalExprError` naming the node: Attribute, Subscript, Lambda, str, Compare, BoolOp, kwargs, starargs.
  - Name must be in `columns`, else `unknown symbol '<x>'`. A Call func must be in `ops` or be `abs`, else `unknown function '<f>'`.
  - Arity: `(x, n)` for every op except `atr(n)` and `rank_pct(x)`.
  - The window arg must be an int `Constant ≥ 1`, else `window must be a positive integer literal` (this covers `lag(x, 0)`, `lag(x, -1)`, `sma(x, 2.5)`, `sma(x, close)`, `sma(x, True)`).
  - `atr` needs `{'high', 'low', 'close'} ⊆ columns`.
  - Every column name must satisfy `isidentifier()`, must not be a keyword, and must not be an op name.
- **Pass 2, sympy.** `parse_expr(expr, local_dict={c: Symbol(c)} | {op: Function(op)} | {'abs': Abs}, global_dict=<only the sympy constructors the standard transformations emit: Symbol, Integer, Float, Rational>, evaluate=False)`.
  - Compile from the resulting tree. Node kinds: Symbol, Number, Add, Mul, Pow, Abs, AppliedUndef.
  - `Pow` with a negative exponent becomes `safe_div(1, base ** |e|, fill=np.nan)`. Other powers run under `np.errstate(invalid='ignore')`.
  - `formula_hash` = `sha256(srepr(parse_expr(..., evaluate=True)))[:16]`.
- **Semantics on wide frames** (`panel[field]` is `date × ticker`):
  - Every windowed op runs per column on `s.dropna()` and is reindexed back (NaN = no bar).
  - `sma` → `indicators.sma`; `ema` → `indicators.ema`; `std` → `rolling(n).std()` (ddof=1); `zscore` → `indicators.zscore`; `rsi` → `indicators.rsi`.
  - `max`/`min` → rolling max/min.
  - `lag(x, n)` → `shift(n)`; `pct_change(x, n)` → `safe_div(s, s.shift(n), fill=np.nan) - 1`.
  - `atr(n)` → per ticker `indicators.atr(hlc.dropna(), n, method='true_range', fallback_pct=None)`.
  - `rank_pct(x)` → `frame.rank(axis=1, pct=True)` (cross-sectional).
  - The output replaces ±inf with NaN.
- **Lookback.**
  - A column counts 1 and a constant 0.
  - `sma`/`std`/`zscore`/`max`/`min`/`ema`: `lb(x) + n − 1`.
  - `rsi`/`pct_change`/`lag`: `lb(x) + n`.
  - `atr(n)`: `n + 1`. `rank_pct`: `lb(x)`.
  - Arithmetic takes the max of its operands.
- **Lint.** For every `ClassDef` body `Assign` to `SIGNAL`, the value must be a str `Constant`, else a `signal_expr` violation `SIGNAL must be a string literal`. `validate_expr_syntax` runs with columns from a sibling `SIGNAL_COLUMNS` literal (set, list, tuple, or `frozenset({...})` of str constants; default `{'close'}`). A `SignalExprError` becomes a `signal_expr` violation. The lint never calls `compile_signal`.

- [ ] **Step 1: Write the failing tests** (`tests/strategies/test_signal_expr.py`; panels use `ZZTEST*` columns)
- `test_rejects_unknown_symbol`. `'sma(vol, 5)'` with `columns={'close'}` raises, matching `unknown symbol 'vol'`.
- `test_rejects_unknown_function`. `'exp(close)'`, `'Integral(close)'` and `'eval(close)'` raise, matching `unknown function`.
- `test_hostile_strings_never_reach_sympy` (Review Focus 1). Monkeypatch `signal_expr.parse_expr` to raise `AssertionError`. Then `"close.__class__"`, `"close[0]"`, `"(lambda: 1)()"`, `"__import__('os')"`, `"sma(close, n=5)"`, `"'a'"` and `"close > 1"` each raise `SignalExprError`.
- `test_rejects_bad_windows_and_arity`. Cover `lag(close, 0)`, `lag(close, -1)`, `sma(close, 2.5)`, `sma(close, close)`, `sma(close, True)`, `sma(close)`, `rank_pct(close, 5)`, `atr(close, 14)`, and `atr(14)` without `high`/`low`.
- `test_sma_is_per_column_dropna_rolling`. The panel has interior NaN gaps; the output column equals `s.dropna().rolling(n).mean().reindex(idx)`.
- `test_rank_pct_is_cross_sectional`, `test_std_is_ddof1`, `test_lag_and_pct_change`
- `test_division_by_zero_is_nan`. `'close / (close - close)'` is all NaN.
- `test_required_lookback`:

  | expression | expected |
  |---|---|
  | `'sma(close, 200)'` | 200 |
  | `'sma(abs(pct_change(close, 1)) / (close * volume), 60)'` | 61 |
  | `'lag(sma(close, 5), 3)'` | 8 |
  | `'atr(14)'` (with hlc columns) | 15 |
  | `'rank_pct(close)'` | 1 |
  | `'ema(close, 10)'` | 10 |
  | `'rsi(close, 14)'` | 15 |
- `test_formula_hash_canonical`. `'close*volume'` and `'volume*close'` hash equal; `'sma(close,20)'` and `'sma(close,21)'` differ; each hash is 16 lowercase hex; and `formula_hash(e) == compile_signal(e, columns=...).formula_hash`.
- `test_no_tail_slicing_for_ema`. The output on a 500-row panel equals `indicators.ema` per column at every row.
- `test_negative_base_fractional_power_is_nan_without_warning`. Run under `warnings.simplefilter('error')`.

In `test_strategy_lint.py`, add `TestSignalExprLint`:
- `test_bad_signal_is_a_violation`
- `test_good_signal_is_clean`
- `test_signal_columns_literal_is_honoured`. `SIGNAL_COLUMNS = frozenset({'close','volume'})` together with a volume expression is clean.
- `test_non_literal_signal_is_a_violation`
- `test_lint_never_imports_sympy`. Run in a subprocess: `python3 -c "import sys; from strategies import strategy_lint as sl; sl.lint_source(src); print('sympy' in sys.modules)"` with `cwd=src`; stdout must be `False`.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_signal_expr.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.signal_expr`).

- [ ] **Step 3: Implement `signal_expr.py` (import sympy lazily inside `compile_signal`/`formula_hash`) and the lint kind**
- [ ] **Step 4: Run both files**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_signal_expr.py -q -W error`
Then: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_strategy_lint.py -q -W error`
Expected: both PASS (`TestFleetIsClean` included; no fleet file declares `SIGNAL`).

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/signal_expr.py src/strategies/strategy_lint.py tests/strategies/test_signal_expr.py tests/strategies/test_strategy_lint.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C7 signal_expr compiler (ast whitelist + sympy parse_expr, formula_hash) + lint kind signal_expr"
```

---

### Task 9: C7b — `BaseStrategy` SIGNAL integration

**Files:**
- Modify: `src/strategies/base.py` (class attributes near `:150-187`; `__init_subclass__` `:189-227`; new methods after `should_exit` `:257-273`)
- Test: `tests/strategies/test_base_signal_dsl.py`

**Interfaces:**
- Consumes: `signal_expr.compile_signal`, `make_panel`, `SignalExprError` (Task 8).
- Produces, on `BaseStrategy`:
  - Class attributes:
    - `SIGNAL: str = ''`
    - `SIGNAL_DIRECTION: str = 'long'` (one of `'long'`, `'short'`, `'both'`)
    - `SIGNAL_THRESHOLD: float | tuple[float, float] = 0.0` (a tuple `(lo, hi)` with `lo < hi` iff `'both'`)
    - `SIGNAL_COLUMNS: frozenset = frozenset({'close'})` (must contain `'close'`)
    - `SIGNAL_BASE_SIZE: float = 0.05`
  - `__init_subclass__`, when `cls.SIGNAL` is non-empty:
    - Compiles it into `cls._compiled_signal`.
    - Validates direction, threshold shape, `'close' ∈ SIGNAL_COLUMNS`, and that `signal_panel` is overridden whenever `SIGNAL_COLUMNS != {'close'}`. Any failure raises `TypeError(f'{cls.__name__}: SIGNAL …: {reason}')` at class definition.
    - Sets `cls.min_lookback = max(cls.min_lookback, required_lookback + 1)`.
    - If `cls.generate_signals` resolves to the abstract `BaseStrategy.generate_signals`, installs `cls.generate_signals = BaseStrategy._signal_dsl_generate`, so `cls.__abstractmethods__` comes out empty. An explicit `generate_signals` on the class always wins.
  - `signal_panel(self, prices, universe, aux_data) -> pd.DataFrame | None`. Default: `make_panel({'close': prices[[t for t in universe if t in prices.columns]]})`. `None` or an empty frame means no signals today.
  - `dsl_should_fire(self, prices, regime) -> bool`. Default `True`.
  - `select_signals(self, values: pd.Series) -> list[tuple[str, str, float]]`, returning `(ticker, 'LONG'|'SHORT', strength)`. Default: decision 4's strict rule; strength = `v − T` for long and `T − v` for short (`v − hi` / `lo − v` for both); sorted by strength descending, ties by ticker ascending; capped at `MAX_SIGNALS`.
  - `_signal_dsl_generate(self, prices, regime, universe, aux_data=None) -> List[Signal]`:
    1. Return `[]` if `prices` is None or empty.
    2. Check, in order, `should_run(regime['state'])`, then `dsl_should_fire`, then `len(prices) >= required_lookback + 1`, then a non-empty panel. On failure, print `skip=regime|gate|lookback|panel_empty` and return `[]`.
    3. `values = compiled(panel).iloc[-1]`, with ±inf → NaN, `dropna()`.
    4. Picks come from `select_signals(values)`.
    5. Each pick becomes a `Signal`:
       - `entry_price = float(panel['close'][t].iloc[-1])`; skip the ticker if it is not > 0.
       - Stops from `compute_stops_and_targets(panel['close'][t].dropna(), dir, price, regime_state=state)`.
       - `position_size_pct = round(SIGNAL_BASE_SIZE * position_scale(state), 4)`, `confidence = 'MED'`.
       - `signal_params = {'dsl_value': round(v, 6), 'formula_hash': h, 'regime': state}`.
  - Logging: exactly one stderr line per call whenever `prices` is non-empty: `[signal_dsl] id=<id> hash=<h> rows=<n> values=<n_finite> long=<n> short=<n>[ skip=<reason>]`. Also the house `[debug] signals=<N>` line.
- `BaseStrategy.generate_signals` keeps `@abstractmethod`.

- [ ] **Step 1: Write the failing tests** (synthetic `ZZTEST*` panels; regime `{'state': 'LOW_VOL'}`)
- `test_dsl_class_is_concrete_and_instantiates`. Covers `__abstractmethods__ == frozenset()`.
- `test_adapters_stay_abstract`. `'generate_signals' in OxfordBaseStrategy.__abstractmethods__` and in `CohortBaseStrategy.__abstractmethods__`.
- `test_bad_signal_fails_at_class_definition`. `pytest.raises(TypeError, match="unknown symbol 'vol'")`.
- `test_bad_direction_threshold_or_missing_close_fail_at_definition`
- `test_extra_columns_require_signal_panel_override`
- `test_min_lookback_auto_raised`. `'sma(close, 200)'` with a declared 20 gives 201; a declared 504 stays 504.
- `test_long_threshold_is_strict`. A value exactly equal to T emits nothing.
- `test_short_and_both_directions`
- `test_max_signals_cap_and_tie_order`
- `test_explicit_generate_signals_wins`
- `test_signal_fields`. Entry price, `signal_params` keys, and stops on the correct side of entry.
- `test_weekend_last_row_emits_nothing_but_logs` (Review Focus 3). The final row is all NaN. Output is `[]`, and stderr has exactly one `[signal_dsl]` line containing `values=0`.
- `test_signal_dsl_line_on_every_skip_path`. Use capsys for the regime, gate, lookback and panel_empty skips.

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_base_signal_dsl.py -q -W error`
Expected: FAIL (a class with `SIGNAL` and no `generate_signals` cannot be instantiated: `TypeError: Can't instantiate abstract class`).

- [ ] **Step 3: Implement in `base.py`.** Import `signal_expr` lazily inside `__init_subclass__`'s SIGNAL branch, so non-DSL imports never load sympy.
- [ ] **Step 4: Run the new file plus the existing base-contract files**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_base_signal_dsl.py -q -W error`
Then: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_exit_hook_interface.py -q -W error`
Then: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_strategy_base_param_merge.py -q -W error`
Expected: all PASS.

- [ ] **Step 5: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/base.py tests/strategies/test_base_signal_dsl.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C7 BaseStrategy SIGNAL/SIGNAL_DIRECTION/SIGNAL_THRESHOLD default generate_signals"
```

---

### Task 10: C7c — three `_dsl` pilots, parity, registration, signatures, prompt

**Files:**
- Create: `src/strategies/implementations/S_golden_cross_trend_signal_tv2_dsl.py`, `S_amihud_illiquidity_premium_dsl.py`, `oxf_bollinger_momentum_dsl.py`, and a `.requirements.json` for each (copy the parent's `required`/`optional`; `strategy_id` = new id).
- Modify: `src/strategies/registry.py` (three `_IMPL_MAP` entries before the closing `}` at `:471`)
- Modify: `src/strategies/generate_signatures.py:53-77` (extract `signature_for_source`)
- Create: `scripts/register_dsl_pilots.py` (modelled on `scripts/register_low_volatility_us_gk63.py`: `--manifest`, `--dry-run` default, `--apply`, `POSTGRES_URI` popped, `_manifest_lock` write, inserts only the three new `candidate` keys with `eligible_regimes` absent, never touches another key)
- Modify: `src/agent/prompts/subagents/strategycoder.md` (two new sections between "### Artifact 4 — Data requirements JSON" and "## Intraday (single-session) strategies", `:162-194`)
- Test: `tests/strategies/test_dsl_pilots.py`, `tests/strategies/test_generate_signatures_formula_hash.py`, `tests/scripts/test_register_dsl_pilots.py`

**Interfaces:**
- Consumes: the Task 9 hooks; `_extra_panels.load_wide` / `liquid_pool`; `OxfordBaseStrategy.basket_ohlc`.
- Produces:
  - **`GoldenCrossTrendStateTV2Dsl(BaseStrategy)`**: `id='S_golden_cross_trend_signal_tv2_dsl'`, `SIGNAL = "(sma(close, 50) - sma(close, 200)) / close"`, `'long'`, threshold `0.0`, `min_lookback=504`, `active_in_regimes=['LOW_VOL','TRANSITIONING']`, `instrument_class='equity'`, `MAX_SIGNALS=50`.
  - **`AmihudIlliquidityPremiumDsl(BaseStrategy)`**
    - Attributes: `id='S_amihud_illiquidity_premium_dsl'`, `SIGNAL_COLUMNS={'close','volume'}`, `SIGNAL = "rank_pct(sma(abs(pct_change(close, 1)) / (close * volume), 60))"`, `'both'`, threshold `(0.20, 0.80)`, `signal_frequency='monthly'`, `min_lookback=90`, `MAX_SIGNALS=24`, the same regimes as the original.
    - `dsl_should_fire` is the original's month-boundary-or-`cadence_reset` gate plus `len(prices) >= min_lookback`.
    - `signal_panel` reproduces the original's pool, `eq`, volume and `len < 30` steps (`S_amihud_illiquidity_premium.py:93-121`) and returns `make_panel({'close': eq[common], 'volume': v[common]})` or `None`.
    - `select_signals` reproduces `:126-138`: `len(values) < 30` → `[]`; `n_leg = min(leg_count, max(1, int(len(values) * 0.20)))`; longs are `v >= 0.80` largest-first `head(n_leg)`, then shorts `v <= 0.20` smallest-first `head(n_leg)`, capped at `MAX_SIGNALS`.
  - **`OxfBollingerMomentumDsl(OxfordBaseStrategy)`**
    - Attributes: `id='oxf_bollinger_momentum_dsl'`, `SIGNAL = "(close - sma(close, 80)) / std(close, 80)"`, `'both'`, `SIGNAL_THRESHOLD = (-B, B)` with `B = 2.0 * math.sqrt(79 / 80)`. This is the original's k=2 on a ddof=0 sigma expressed through ddof=1 `std`; the derivation goes in a comment.
    - `min_lookback=100`, `MAX_SIGNALS=25`, `active_in_regimes=['LOW_VOL','TRANSITIONING']`.
    - `signal_panel` builds `close` from `self.basket_ohlc(prices)` and ignores `universe`.
  - Each pilot's docstring cites its original and lists the divergences: a ticker absent on the evaluation row emits nothing (the original uses its stale last value); Amihud averages the last 60 valid observations (the original takes valid cells among the last 60 rows, needing ≥ 40); Bollinger reads the union basket calendar.
  - `generate_signatures.signature_for_source(source: str) -> dict`: the existing per-file dict. When the source has a class-body `SIGNAL = "<literal>"` (found via `ast`, never imported), it adds `'formula_hash': signal_expr.formula_hash(expr)` and sets `formula_tokens` to the sorted op names and column names used. `main()` calls it and is otherwise unchanged.

- [ ] **Step 1: Write the failing tests**

`tests/strategies/test_dsl_pilots.py` uses deterministic synthetic panels (`ZZTEST1..ZZTEST40`, business days, seeded GBM with per-ticker drift regimes so crosses and breakouts occur). Parity compares `{(date, ticker, direction)}` from the original's and the pilot's `generate_signals`, each called on `prices.iloc[:i+1]` for every evaluation date.
- `test_golden_cross_parity`. Twelve tickers, 450 rows, evaluated on every row from 200 on. Sets are equal at every date. At least one date has signals and at least one cross date occurs.
- `test_amihud_parity`
  - 40 tickers, 300 rows, no NaN, all volume > 0.
  - Monkeypatch `load_wide` (returns the synthetic volume) and `liquid_pool` (returns all 40) in both modules' namespaces.
  - Evaluate at every row; only month boundaries emit. Sets are equal, and both legs are non-empty on at least one boundary.
- `test_bollinger_parity`. Seed `_ohlc_cache` on both instances with synthetic OHLC for 10 `OXFORD_ETF_BASKET` tickers (identical dates), so nothing reads parquet. Evaluate every row from 100 on. Sets are equal, and both directions occur.
- `test_pilot_ids_match_stems_and_registry`. Each class `id` equals its file stem, and `_IMPL_MAP[id] == ('strategies.implementations.<id>', '<Class>')`.
- `test_pilot_requirements_mirror_parents`
- `test_pilots_lint_clean`. `strategy_lint.lint_file(path) == []` for each pilot.

`tests/strategies/test_generate_signatures_formula_hash.py`:
- `test_signature_for_source_prefers_formula_hash`. The DSL source entry has `formula_hash == signal_expr.formula_hash(expr)` and `formula_tokens == ['close', 'sma']` for `"sma(close, 20) - close"`.
- `test_non_dsl_source_unchanged`. Same output as the old substring path for a source containing `'zscore'` and `'LONG'`.
- `test_main_not_called`. Monkeypatch `SIG_OUT` to `tmp_path` and assert the real file's mtime is untouched.

`tests/scripts/test_register_dsl_pilots.py`, against a `tmp_path` copy of a minimal manifest only:
- `test_dry_run_writes_nothing`
- `test_apply_inserts_three_candidates_only`. The diff outside the three keys is empty; `state == 'candidate'`; no `eligible_regimes`.
- `test_apply_is_idempotent`

- [ ] **Step 2: Run and confirm failure**

Run: `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_dsl_pilots.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: ...S_golden_cross_trend_signal_tv2_dsl`).

- [ ] **Step 3: Implement the pilots, sidecars, `_IMPL_MAP` entries, `signature_for_source`, and the register script**
- [ ] **Step 4: Write the two prompt sections**

`## Shared helpers` (≤ 25 lines):
- Name `strategies.indicators` (`atr` methods and the house `close_diff` default), `strategies.safe_ops` (`EPS`, `safe_div` instead of raw `1e-…` literals), `strategies.windows` (`event_window(..., close_unfinished=...)`, `state_window`, `statecount`, `crossing`, `episodes_to_trades` for `__main__` quick backtests) and `strategies.resample.session_bars` (never `pd.BDay` as a session calendar).
- Say the four WARN lint rules exist and will become FAIL after the fleet sweep.

`## Expression DSL (SIGNAL = "...")` (≤ 30 lines):
- When to use it: rolling-op + rank + threshold rules.
- The op list with arities, and the column and `signal_panel` contract.
- The direction and threshold semantics, strict comparisons, and the NaN = no bar rule.
- The three pilots as worked examples.
- When it is not expressible (fits, optimisers, recursions), write Python as before.

- [ ] **Step 5: Run the three test files**

Run each of:
- `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_dsl_pilots.py -q -W error`
- `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/strategies/test_generate_signatures_formula_hash.py -q -W error`
- `nice -n 19 python3 -m pytest /root/openclaw/.claude/worktrees/six-c/tests/scripts/test_register_dsl_pilots.py -q -W error`

Expected: all PASS.

Do NOT run `tests/strategies/test_oxf_contract.py`. It sweeps every `oxf_*` module, including the new pilot, through the real basket loader, a ticker-filtered `prices.parquet` read. It is left to the merged-tree run (§7).

- [ ] **Step 6: Commit**
```bash
git -C /root/openclaw/.claude/worktrees/six-c add src/strategies/implementations/S_golden_cross_trend_signal_tv2_dsl.py src/strategies/implementations/S_golden_cross_trend_signal_tv2_dsl.requirements.json src/strategies/implementations/S_amihud_illiquidity_premium_dsl.py src/strategies/implementations/S_amihud_illiquidity_premium_dsl.requirements.json src/strategies/implementations/oxf_bollinger_momentum_dsl.py src/strategies/implementations/oxf_bollinger_momentum_dsl.requirements.json src/strategies/registry.py src/strategies/generate_signatures.py scripts/register_dsl_pilots.py src/agent/prompts/subagents/strategycoder.md tests/strategies/test_dsl_pilots.py tests/strategies/test_generate_signatures_formula_hash.py tests/scripts/test_register_dsl_pilots.py
```
```bash
git -C /root/openclaw/.claude/worktrees/six-c commit -m "feat(strategies): C7 pilots S_golden_cross_trend_signal_tv2_dsl / S_amihud_illiquidity_premium_dsl / oxf_bollinger_momentum_dsl + parity, formula_hash signatures, register script, StrategyCoder DSL+helpers sections"
```

**Controller follow-ups (not implementer steps):**
- After the stream merges, run `scripts/register_dsl_pilots.py --apply` against main's manifest (§6.5).
- The weekly `cron-schedule.js:870` regeneration picks up the three signature entries.
- Recording the fleet census (`python -m strategies.strategy_lint --census`) in `docs/archive/changelog.md` is the precondition for ever passing `--strict` from the orchestrator.
- Out of scope: switching the dedup consumers (`research/fingerprint_dedup.py`, `_candidate_dedup.js`) to exact `formula_hash` matching.
