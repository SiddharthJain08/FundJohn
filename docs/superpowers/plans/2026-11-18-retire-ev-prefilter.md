# Retire EV / p_t1 Prefilter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the direction-blind EV/p_t1 prefilter from the handoff, the two day-level quality gates that read its summary, and the EV/p_t1 fields everywhere they are emitted, while keeping the summary row, OUE classification and reports working.

**Architecture:** Three deletions with their consumers fixed in the same task: (1) the handoff builder's GBM block + prefilter reasons + summary writer; (2) the executor and orchestrator `check_signal_quality` gates + the Node health digest line; (3) report cards and the sized-order payload fields. OUE keeps a baseline of 0 when `ev_gbm` is absent.

**Tech Stack:** Python 3 (pytest, DB stubs), Node (`node --test`).

**Spec:** `docs/superpowers/specs/2026-11-18-retire-ev-prefilter-design.md`

## Global Constraints

- `daily_signal_summary` schema is untouched (append-only canonical-style table); the INSERT must still satisfy NOT NULL `n_signals`, `ev_pos`, `ev_neg`, `high_conv_count` (write 0 for the three retired counters) and write `avg_ev = NULL`, `avg_p_t1 = NULL`, metadata JSON `{"green": …, "prefiltered": …, "d1_matches": …, "ev_gate": "retired 2026-11-18"}`.
- The only prefilter reason that may remain is `untradable_at_alpaca`. `veto_log` rows for it keep today's shape.
- `hv21` must remain in every enriched handoff signal (OUE). `oue_classifier._load_signal_ev` returns `{'ev_gbm': 0.0, 'hv21': hv}` when the row has `hv21` but no `ev_gbm`.
- Shorts: `risk_pct`, `t1_pct`, `t2_pct`, `rr1` must be positive magnitudes for a sane short bracket (stop > entry > t1), unchanged for longs.
- No step may read `daily_signal_summary` to decide whether to trade. The `[handoff] prefilter:` log line keeps the prefix `prefilter: N green / M filtered (` and the `untradable=…, universe_size=…)` tokens; the `min_ev=…, min_p_t1=…` tokens go.
- Box rules: light tests only; no full suite; separate `git add` / `git commit -F` calls; message files, no backticks in the shell. Tests reach the real DB at import — stub every DB touchpoint.

## Review Focus

1. A SHORT signal with stop above entry and target below must be GREEN and carry positive `risk_pct`/`t1_pct`/`rr1` (Task 1 `test_short_signal_is_green_with_positive_geometry`).
2. A handoff day where every signal is untradable must still insert a summary row with `n_signals = len(enriched)` and zeros, not NULL, in the NOT NULL counters (Task 1 `test_summary_row_writes_zero_counters_and_null_ev`).
3. The alpaca step on a day whose latest summary row has `avg_ev = NULL` and `ev_pos = 0` must proceed (Task 2 `test_alpaca_step_has_no_signal_quality_gate`).
4. OUE classification for a signal whose handoff row lacks `ev_gbm` must return baseline 0, not None/skip (Task 1 `test_load_signal_ev_defaults_missing_ev_to_zero`).
5. The health digest on a row with `avg_ev = NULL` must not print `NaN%` (Task 2 node test `digest line has no EV when the gate is retired`).

---

### Task 1: Handoff builder — remove the GBM block and the EV/p_t1 gate; summary row; OUE baseline

**Files:**
- Modify: `src/execution/trade_handoff_builder.py:40-80` (constants), `:550-690` (`compute_features`), `:795-835` (prefilter block + log line), `:920-965` (summary writer)
- Modify: `src/execution/oue_classifier.py:48-70` (`_load_signal_ev`)
- Test: `tests/execution/test_trade_handoff_universe_filter.py` (extend), new `tests/execution/test_trade_handoff_prefilter_retired.py`, `tests/execution/test_engine_oue_ordering.py` or new `tests/execution/test_oue_missing_ev_baseline.py`

**Interfaces:**
- Produces: `compute_features(sig, px, spy_returns, run_date) -> dict | None` without keys `ev_gbm`, `p_t1`, `exp_exit_days`, `exp_exit_date`; keys `risk_pct`, `t1_pct`, `t2_pct`, `rr1` are direction-aware magnitudes (`sgn = -1 if direction upper == 'SHORT' else 1`; `risk_pts = sgn*(entry − stop)`, `t1_pts = sgn*(t1 − entry)`, `t2_pts = sgn*(t2 − entry)` when t2 given else `2*t1_pts`); `hv21/hv63/hv252/beta_spy/rsi14/mom_*/size_pct/confidence/entry/stop/t1/t2/direction/strategy_features` unchanged. Module constants `MIN_EV_GBM`, `MIN_P_T1`, `HOLDING_PERIOD`, `DEFAULT_HP` deleted (grep confirms no external user; `signal_cadence_gate.py` only mentions the pattern in a comment).
- Prefilter: `prefiltered` entries carry `{'ticker','strategy_id','direction','reason':'untradable_at_alpaca'}` only; every tradable enriched signal goes to `green` (sentiment attached as today). Log: `[handoff] prefilter: {len(green)} green / {len(prefiltered)} filtered (untradable={n}, universe_size={…})`.
- Summary INSERT per Global Constraints; the log line becomes `[handoff] daily_signal_summary appended: n={len(enriched)} (ev gate retired)`.
- `_load_signal_ev(signal_date, ticker, strategy_id)` → `{'ev_gbm': float(ev) if ev is not None else 0.0, 'hv21': float(hv)}` when `hv21` present; `None` only when `hv21` is missing or the file/row is absent.

- [ ] **Step 1: Write the failing tests** (`tests/execution/test_trade_handoff_prefilter_retired.py`; stub `psycopg2.connect` / `load_tradable_universe` / `_load_sentiment_for_tickers` / `load_prices` the way `test_trade_handoff_universe_filter.py` does)

```python
def test_short_signal_is_green_with_positive_geometry(px_two_tickers):
    sig = {'ticker': 'AAA', 'strategy_id': 'S_x', 'direction': 'SHORT', 'entry_price': 100.0, 'stop_loss': 103.0, 'target_1': 94.0, 'target_2': 88.0, 'signal_params': {}}
    f = hb.compute_features(sig, px_two_tickers, spy_returns, '2026-11-17')
    assert f['risk_pct'] == pytest.approx(0.03) and f['t1_pct'] == pytest.approx(0.06) and f['rr1'] == pytest.approx(2.0)
    assert 'ev_gbm' not in f and 'p_t1' not in f and 'exp_exit_days' not in f and 'hv21' in f

def test_long_geometry_unchanged(px_two_tickers):
    sig = {..., 'direction': 'LONG', 'entry_price': 100.0, 'stop_loss': 97.0, 'target_1': 106.0, ...}
    f = hb.compute_features(sig, px_two_tickers, spy_returns, '2026-11-17')
    assert f['risk_pct'] == pytest.approx(0.03) and f['t1_pct'] == pytest.approx(0.06)

def test_build_prefilters_only_untradable(monkeypatch, ...):
    # two tradable signals (one LONG one SHORT with poor R:R) + one untradable ticker
    out = hb.build('2026-11-17')
    assert {p['reason'] for p in out['prefiltered']} == {'untradable_at_alpaca'}
    assert len(out['signals']) == 2
    assert not hasattr(hb, 'MIN_EV_GBM') and not hasattr(hb, 'MIN_P_T1')

def test_summary_row_writes_zero_counters_and_null_ev(monkeypatch, ...):
    # capture the INSERT params via a stub cursor
    params = captured_insert_params()
    run_date, regime, n, ev_pos, ev_neg, avg_ev, avg_p_t1, high_conv, meta = params
    assert (ev_pos, ev_neg, high_conv) == (0, 0, 0) and avg_ev is None and avg_p_t1 is None
    assert json.loads(meta)['ev_gate'] == 'retired 2026-11-18' and n == 3
```
and in a small OUE test:
```python
def test_load_signal_ev_defaults_missing_ev_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(oue, 'HANDOFF_DIR', tmp_path)
    (tmp_path / '2026-11-17_structured.json').write_text(json.dumps({'signals': [{'ticker': 'AAA', 'strategy_id': 'S_x', 'hv21': 0.4}]}))
    assert oue._load_signal_ev('2026-11-17', 'AAA', 'S_x') == {'ev_gbm': 0.0, 'hv21': 0.4}
    (tmp_path / '2026-11-16_structured.json').write_text(json.dumps({'signals': [{'ticker': 'AAA', 'strategy_id': 'S_x'}]}))
    assert oue._load_signal_ev('2026-11-16', 'AAA', 'S_x') is None
```

- [ ] **Step 2: Run to verify they fail** — `PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/execution/test_trade_handoff_prefilter_retired.py tests/execution/test_oue_missing_ev_baseline.py -v` → FAIL (`ev_gbm` present / `prefilter_low_pt1` reason / `None` from the loader).
- [ ] **Step 3: Implement** per Interfaces. Delete the GBM block wholesale (lines ~608-646 today), the constants, and the two prefilter branches; keep the tradability branch; keep `hv21`.
- [ ] **Step 4: Run** `PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/execution/test_trade_handoff_prefilter_retired.py tests/execution/test_oue_missing_ev_baseline.py tests/execution/test_trade_handoff_universe_filter.py tests/execution/test_trade_handoff_builder_sentiment.py tests/execution/test_handoff_regime_source.py tests/execution/test_handoff_portfolio_value_live.py tests/execution/test_handoff_datahub_migration.py tests/execution/test_engine_oue_ordering.py tests/execution/test_send_report_closed_digest.py` → all PASS.
- [ ] **Step 5: Commit** — `handoff: retire the EV/p_t1 prefilter (direction-blind GBM); summary row keeps n only; OUE baseline 0 without ev_gbm`.

---

### Task 2: Remove both `check_signal_quality` gates; health digest line

**Files:**
- Modify: `src/execution/alpaca_executor.py:337-355` (delete `check_signal_quality`), `:2980-2984` (call site → delete; keep the `conn` open for later use)
- Modify: `src/execution/pipeline_orchestrator.py:478-540` (delete `SIGNAL_QUALITY_THRESHOLDS`, `check_signal_quality`), `:1073-1082` (delete the gate block before `trade`)
- Modify: `src/engine/daily-health-digest.js:70-74, 105-141`
- Test: `tests/execution/test_pipeline_orchestrator_steps.py:105` (the patch target disappears — replace with a test that the trade step runs with no summary gate), new `tests/execution/test_alpaca_executor_no_quality_gate.py`, new `tests/engine/daily_health_digest_gate_retired.test.js` (or extend an existing digest test if one exists — implementer checks `ls tests | grep -i digest`)

**Interfaces:**
- `pipeline_orchestrator` no longer defines `check_signal_quality` / `SIGNAL_QUALITY_THRESHOLDS`; the module docstring's step description drops the gate.
- `alpaca_executor` no longer defines `check_signal_quality`; the log line `Signal-quality OK — …` is gone; a one-line `log('signal-quality gate retired 2026-11-18 (handoff EV prefilter removed)')` is acceptable but not required.
- Health digest: `gateLine` = `🚨 Latest run (${runDay}): **0 signals** …` when `n_signals == 0` (unchanged text), else `📈 Latest run (${runDay}): ${totalCnt} signals${staleTag}`; `deltaLine` = `   Δ vs prior: ${d} signals`; the SQL drops `ev_pos, avg_ev, high_conv_count` from the SELECTs. No `NaN` anywhere.

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_alpaca_executor_no_quality_gate.py
def test_executor_module_has_no_signal_quality_gate():
    import execution.alpaca_executor as ex
    assert not hasattr(ex, 'check_signal_quality')
    src = inspect.getsource(ex)
    assert 'daily_signal_summary' not in src

# tests/execution/test_pipeline_orchestrator_steps.py — replace the patch of check_signal_quality with:
def test_orchestrator_has_no_signal_quality_gate():
    import execution.pipeline_orchestrator as po
    assert not hasattr(po, 'check_signal_quality') and not hasattr(po, 'SIGNAL_QUALITY_THRESHOLDS')
    assert 'daily_signal_summary' not in inspect.getsource(po)
```
```js
// digest: feed a fake dbQuery returning {n_signals: 12, avg_ev: null, ev_pos: 0, high_conv_count: 0}
test('digest line has no EV when the gate is retired', async () => { const txt = await buildDigest(...); assert.match(txt, /Latest run \(\d{4}-\d{2}-\d{2}\): 12 signals/); assert.doesNotMatch(txt, /NaN|avg EV|green|high-conv|quality gate/); });
```
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Delete, don't comment out. Keep the `psycopg2.connect(uri)` in the executor's `main()` if later code uses `conn` (it does: `already_executed`, submissions).
- [ ] **Step 4: Run** `PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/execution/test_pipeline_orchestrator_steps.py tests/execution/test_alpaca_executor_no_quality_gate.py tests/execution/test_alpaca_executor_dtbp_guard.py tests/execution/test_executor_handoff_freshness.py` and `node --test <digest test>` → all PASS.
- [ ] **Step 5: Commit** — `orchestrator/executor: remove the signal-quality kill switches; health digest drops EV/green`.

---

### Task 3: Report cards and sized-order payload drop `ev`/`p_t1`; docs

**Files:**
- Modify: `src/execution/send_report.py:160-175` (greenlist card: drop the `ev`/`p_t1` cells), `:255-270` (table: drop the `ev_gbm` column and its header)
- Modify: `src/execution/regime_blended_sizer.py:1450, 3848, 3939, 3965` and `src/execution/regime_blended_sizer_live.py:12, 200, 231, 311` (drop the `'ev'`/`'p_t1'` keys from emitted orders and the docstring)
- Modify: `docs/archive/changelog.md` (newest-first entry), `/root/openclaw/CLAUDE.md` is the controller's (not this task)
- Test: `tests/execution/test_greenlist_cards.py` (card without ev/p), `tests/execution/test_regime_blended_sizer_live.py`, `tests/execution/test_sizer_live_eod_main.py`, `tests/execution/test_option_expiry_mgmt.py`, `tests/execution/test_option_close_path.py`, `tests/execution/test_crypto_redeploy_pooled.py`, `tests/execution/test_regime_blended_sizer.py` (fixtures that *supply* `ev`/`p_t1` may keep them; any assertion on the emitted keys is updated)

**Interfaces:**
- Emitted order dicts (`_sharpe_cadence_path`, `_emit_orders_from_targets`, the live wrapper's `_build_sized_payload`) no longer contain `ev` or `p_t1`. `sized_handoff.finalize_sized_payload` is unchanged (it does not read them — implementer greps to confirm).
- Greenlist card: same layout minus the EV/p columns; `_greenlist_card` and the digest table keep every other column.

- [ ] **Step 1: Write the failing tests** — assert `'ev' not in order and 'p_t1' not in order` for one emitted order in `test_regime_blended_sizer_live.py` and `test_crypto_redeploy_pooled.py`; assert the greenlist card text has no `EV` / `p(` tokens in `test_greenlist_cards.py`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Grep `tests/` for `['ev']` / `o.get('ev')` / `'p_t1'` assertions and update only those.
- [ ] **Step 4: Run** the seven test files above plus `tests/execution/test_sized_handoff_guard.py` → all PASS. Add the changelog entry (cite the three commits' subjects).
- [ ] **Step 5: Commit** — `reports/sizer: drop ev/p_t1 fields from cards and sized orders; changelog`.

---

## Self-review notes
- Spec G1/G2/G4/G6 → Task 1; G3/G5 → Task 2; G7 + docs → Task 3. Non-goals respected (no `execution_signals`/`veto_log` schema changes; `trade_agent.py` untouched).
- Names: `check_signal_quality` (both modules), `_load_signal_ev`, `compute_features`, `_greenlist_card`, `buildDigest`.
- Review Focus 1–5 each have a named test in Tasks 1–2.
