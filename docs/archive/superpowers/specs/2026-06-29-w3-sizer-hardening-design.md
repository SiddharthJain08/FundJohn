# W3 — Sizer Hardening (HIGH fixes) Design Spec

- **Date:** 2026-06-29
- **Branch:** feat/intraday-regime-15min-prefetch
- **Status:** Approved (design) — pending spec review → implementation plan
- **Workstream:** W3 remediation (the 2 HIGH findings from the W3 sizing-verification recon). W1 + W2 done & live.
- **Source of record:** W3 recon in `.superpowers/sdd/progress.md`; verdict in memory `project_w3_sizing_verification`.

## §0 Context & Goal
The W3 recon (3 read-only agents + live checks) found the live sizer fundamentally SOUND (core math correct; weights/config fresh; DTBP guard, circuit breaker, EOD→open lane all solid) with all findings LATENT. Two HIGH hardening fixes are approved to build now; MED/LOW are a deferred backlog (§6). The fixes touch the LIVE sizing + intraday-redeploy path → TDD + gated deploy. Live sizer = `src/execution/regime_blended_sizer_live.py` (`main()`) + engine `src/execution/regime_blended_sizer.py` (`size_positions` / `_sharpe_cadence_path`). Intraday redeploy driver = `scripts/redeploy_pipeline.py`.

## §1 Decisions Locked (operator, 2026-06-29)
- **F1** account-fetch failure → **ABORT the sizer run + alert** (emit zero orders), not fabricate $100k.
- **F2a** intraday-redeploy gate → **signal-count floor vs recent baseline** (abort the redeploy's sizing when the active signal set is abnormally thin; do NOT orphan-close on bad/thin data).
- **F2b** wire the dead `position_sizing_lambda_intraday` → intraday redeploys size at **1×**, overnight/EOD at 1.85× — via an **env flag** set by `redeploy_pipeline`.
- **F2c** per-ticker cap on intraday → **extend the EXISTING conviction cap** (`0.05×|sharpe|×λ×NAV`) to the intraday-redeploy path (reuse F2b's flag); the plain daily cadence path stays byte-identical.

## §2 Architecture / new units
- New pure helper `src/execution/signal_set_health.py` — `is_signal_set_thin(current_count, baseline_count, floor, frac) -> bool` (+ a `recent_baseline(counts) -> float` median helper). Pure, unit-testable.
- One shared env flag, **`OPENCLAW_INTRADAY_REDEPLOY=1`**, set by `redeploy_pipeline._spawn_orchestrator` on the trade step it spawns. Read by `_load_lambda` (F2b) and the conviction-cap gate (F2c) and the F2a gate. Mirrors the existing `OPENCLAW_EOD_RECONCILE` pattern.
- F1 lives in `regime_blended_sizer_live.main()`; F2a lives in the sizer engine on the intraday path (where orphan-closes originate); F2b/F2c are small gated edits in the engine.

## §3 F1 — Abort on account-fetch failure
`regime_blended_sizer_live.py:492-501`: the `except` fabricates `account = {'equity': 100_000.0, ...}` then `equity = float(account.get('equity'))` and proceeds to `size_positions`. **Change:** in the `except`, do NOT fabricate; set `account = None`. Immediately after the try/except, add:
```python
    if account is None or not account.get('fetched', True):
        msg = '[regime_blended_sizer_live] ABORT: account fetch failed — emitting ZERO orders (no sizing against fabricated equity)'
        print(msg, file=sys.stderr)
        try:
            from src.execution.pipeline_orchestrator import post_channel
            post_channel(os.environ.get('OPENCLAW_TRADE_ALERT_WEBHOOK_NAME', 'trade-reports'), '🛑 ' + msg)
        except Exception as _e:
            print(f'  (alert post failed: {_e})', file=sys.stderr)
        return   # zero orders; no handoff write; next cycle retries
```
(Confirm `main()` returns cleanly here without writing a sized handoff — the handoff write must be AFTER this point. `_fetch_account_state` sets `fetched=False` on its own soft-failures, so the `not account.get('fetched', True)` clause also catches a soft-failed fetch that returned a dict.) **No fabricated-equity path remains.**

## §4 F2b — wire intraday λ
`_load_lambda` (`rbs.py:165-182`) currently reads only `position_sizing_lambda`. **Change the signature to `_load_lambda(default=2.0, *, intraday=False)`** and select the key:
```python
    key = 'position_sizing_lambda_intraday' if intraday else 'position_sizing_lambda'
    ... cur.execute("SELECT value FROM pipeline_config WHERE key = %s", (key,)) ...
```
At the call site (`rbs.py:906`), pass `intraday=(os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1')`. `redeploy_pipeline._spawn_orchestrator` (`scripts/redeploy_pipeline.py:262`) sets `OPENCLAW_INTRADAY_REDEPLOY=1` in the spawned process env (alongside the existing env). Keep the `max(0.10, min(2.00, v))` clamp. (Note the F6-backlog item: default stays 2.0; not changed here.)

## §5 F2c — extend the per-ticker conviction cap to the intraday path
`rbs.py:1154`: `if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':` gates the conviction cap. **Change to:**
```python
    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1' or os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1':
```
The cap math (`PER_TICKER_CAP_SHARPE_FRAC × |gate_net_sharpe| × lam × nav`) is unchanged; on the intraday path `lam` is now the intraday λ (F2b), so the cap scales correctly. The plain daily cadence path (neither flag) remains byte-identical (uncapped), per the existing comment's intent.

## §5b F2a — signal-set-health gate (no orphan-close on thin data)
The blowout: an intraday redeploy with an abnormally thin active-signal set orphan-closes the uncovered held positions at the open. **Gate it in the sizer's intraday path** (where the orphan-close is computed), reusing the `OPENCLAW_INTRADAY_REDEPLOY` flag. In `_sharpe_cadence_path`, after the active signal set is loaded and before `_classify_position_deltas`:
```python
    if os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1':
        from src.execution.signal_set_health import is_signal_set_thin, recent_baseline
        baseline = recent_baseline(_recent_active_counts(conn, lookback=10))  # median of last 10 cycles' active-set sizes
        if is_signal_set_thin(len(active), baseline, floor=SIGNAL_SET_MIN_FLOOR, frac=SIGNAL_SET_MIN_FRAC):
            logger.warning('ABORT intraday redeploy sizing: active signals=%d < gate (floor=%d, %.0f%%×baseline=%.0f) — leaving book intact',
                           len(active), SIGNAL_SET_MIN_FLOOR, SIGNAL_SET_MIN_FRAC*100, baseline)
            # alert + emit ZERO orders (no orphan-closes); next cycle retries
            return []
```
- `is_signal_set_thin(current, baseline, floor, frac)` → `current < max(floor, frac*baseline)` (with `baseline<=0` → use `floor` only).
- Proposed defaults: **`SIGNAL_SET_MIN_FLOOR=10`, `SIGNAL_SET_MIN_FRAC=0.30`** (abort if the active set is <10 signals OR <30% of the recent-10-cycle median). Tunable; documented constants.
- `_recent_active_counts` reads recent active-set sizes from `execution_signals` (count of the active/open set per recent signal_date/cycle). Baseline = median.
- Zero-signal case is already guarded upstream (`ticker_w={}`→`return []`); this catches the *partial-thin* case. A legitimate large de-risk (healthy count, large rebalance) is NOT blocked — the gate is on signal COUNT, not orphan fraction.

## §6 Testing (TDD)
- `tests/test_signal_set_health.py` (pytest — the sizer is Python): `is_signal_set_thin` cases (below floor; below frac×baseline; healthy; baseline<=0 → floor-only) + `recent_baseline` median. Pure, no DB.
- F2b: `tests/test_load_lambda_intraday.py` — monkeypatch the DB read (or use a temp pipeline_config row in a rolled-back txn, mirroring test_migration_139) to assert `_load_lambda(intraday=True)` reads the intraday key, `intraday=False` reads the overnight key.
- F2c: a focused test that with `OPENCLAW_INTRADAY_REDEPLOY=1` the conviction cap clamps an over-cap ticker (and with neither flag it does not). Use the existing sizer test harness/pattern.
- F1: extract the abort decision into a tiny testable predicate if practical, or cover via a focused test that a failed/absent account → zero orders; otherwise verify by inspection + the SDD reviewer (note in the plan).
- Respect VPS 2-core: tests are light.

## §7 Sequencing & commit plan (path-scoped, W2-style)
1. C1 — `signal_set_health.py` + test [TDD] (pure helper; no live touch).
2. C2 — F2b `_load_lambda(intraday=)` + call-site + redeploy env flag [TDD].
3. C3 — F2c extend conviction-cap gate [TDD].
4. C4 — F2a wire the health gate into `_sharpe_cadence_path` intraday path + `_recent_active_counts` [TDD on the helper; integration by inspection].
5. C5 — F1 abort-on-fetch-fail in `sizer_live.main()` [test/inspection].
Each commit path-scoped (guard the staged set); the live tree carries the WIP (manifest.json/registry.py/implementations/S_*) — never stage it. Footer on every commit.

## §8 Gated deploy (operator-approved step, AFTER review — same posture as W2)
Commits land on `feat/intraday-regime-15min-prefetch` (not pushed/restarted until operator approves). To go live: restart user-scope `johnbot.service` (NOT system unit). No DB migration this time. The intraday-λ + cap + gate only fire on the intraday-redeploy path (env-flagged), so the daily/EOD lane is unchanged.

## §9 Deferred — W3 sizer-hardening backlog (NOT this spec)
regime split-source (sizer DB vs engine file); silent fail-opens → fail-loud (cluster-cap scipy, broker-fetch flat-book, weights/signal-quality age-gates); confirmer all-regimes (gate vs doc); `_load_lambda` DB-fail fallback 2.0→1.85; verify prefetch `--episode` end-to-end; dead `b1_order_source` 25%-cap code. Tracked in `project_w3_sizing_verification`.

## §10 Risk controls
- No fix changes the daily/EOD lane behavior except F2c's cap which already applied in EOD mode (unchanged there) — the NEW activation is intraday-only, env-flagged.
- F1 fails SAFE (zero orders > wrong orders). F2a fails SAFE (leave book intact > orphan-close on bad data).
- Path-scoped commits; no master-data touch; no live-book mutation (these change sizing LOGIC, applied on the next gated restart).
- The F2a defaults (floor=10, frac=0.30) are conservative starting values; surface them as tunable constants + log every abort for calibration.
