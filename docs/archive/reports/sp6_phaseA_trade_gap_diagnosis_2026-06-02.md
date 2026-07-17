# SP-6 Phase A — into-close `trade` step aborts (no handoff). Diagnosis + fix spec

**Found:** 2026-06-02, first live day. **Status:** SAFE to leave armed (paper; no destructive path; positions carry). Fix deliberately (brainstorm/TDD) — do NOT hot-patch the live trade path overnight.

## Symptom
The 3:55 PM `eod-into-close-fill` cron (`['trade','alpaca','reconcile','report','health']`) **aborts at `trade`** every day: `step=trade rc=1 timedOut=false`, empty stderr, orchestrator logs `status=aborted aborted=trade`. Because `trade` aborts, `alpaca`/`reconcile`/`report`/`health` never run ⇒ **0 broker orders placed**. (The 16 `execution_signals` marked `FILLED` @20:26 are reconcile-marks vs the carried 8-position book, NOT new fills — broker shows 0 orders today.) No destructive event: 8 positions safely carried; the empty-signals→flatten path is not armed (550 APPROVED present).

## Root cause
The `trade` step = `src/execution/regime_blended_sizer_live.py` (the production-sizer **driver**). Its `main()`:
```
208  handoff = read_handoff(run_date_str, 'structured')
209  if handoff is None:
210      print('... no handoff for {date}; nothing to size')
211      return 1                      # <-- rc=1 abort happens HERE
213  signals = handoff.get('signals', [])
215  if not signals: ... 'nothing to do'   # second handoff-dependent bail
```
But the SP-6 EOD compute (`eod-signal-register`, 4:15 PM) dispatches only `['collect','sentiment','signals']` — **no `handoff` step** (unlike the legacy `scheduled-compute` = `['collect','sentiment','signals','ic_gate','handoff','trade']`). The carried set is written to **`execution_signals`** as `COMPUTED`→`APPROVED`; **no structured handoff is ever produced** (last `output/handoffs/*.json` = 2026-05-28; Redis key `handoff:{date}:structured` absent). So `read_handoff` returns `None` → `return 1`.

**The intended design (design doc §"Option B", line 196):** *"in EOD mode (`OPENCLAW_EOD_RECONCILE=1`) the sizer loads the APPROVED carried set (`lifecycle_state='APPROVED' AND target_date=today`) and bypasses the cadence gate."* This is **Task 8a**.

**What actually shipped:** Task 8a was implemented in the **library** `regime_blended_sizer.py`, NOT the driver:
- `_load_approved_carried_signals(weight_by_strat)` (line 195) loads the APPROVED carried set.
- `sharpe_cadence()` (lines 510-513) self-selects it: `if os.environ.get('OPENCLAW_EOD_RECONCILE')=='1': active = _load_approved_carried_signals(...)` — it **ignores the passed `signals`** in EOD mode, and correctly returns `[]` (zero orders) on an empty APPROVED set (flatten handled by `run_reconcile`).

So the library is correct and EOD-aware. **The driver `regime_blended_sizer_live.py:main()` was never made EOD-aware** — it still requires a legacy handoff and bails (line 211) *before* calling `size_positions()` (line 295), so the library's EOD path is unreachable from the live cron.

## Why it wasn't caught
- Task 8a's tests covered the **library** EOD-loading (`regime_blended_sizer.sharpe_cadence`) directly — which works.
- The **driver's** EOD path (`main()` with no handoff) had no coverage; gate-off legacy tests stayed green (byte-identical) and gave false confidence.
- Rollout shadow/dry-run steps (design §156 Steps 2-4) were **skipped** (structurally can't run pre-EOD — see Phase A memory). The dry-run path short-circuits (`[regime_blended_sizer_live] dry-run skip`). So **today was the first time the `trade` step ran live** → exposed the gap.

## Fix (deliberate, tomorrow — TDD)
Make `regime_blended_sizer_live.py:main()` EOD-aware, mirroring the library:
```python
if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
    # SP-6 EOD mode: the carried set lives in execution_signals (APPROVED), not a
    # legacy handoff. Synthesize a minimal handoff; the library (sharpe_cadence)
    # self-loads the APPROVED carried set and ignores `signals`.
    regime_state = _load_regime_from_db(...)          # market_regime fallback (already used at line ~248)
    handoff = {'signals': [], 'cycle_date': run_date_str, 'regime': {'state': regime_state}}
    signals = []
else:
    handoff = read_handoff(run_date_str, 'structured')
    if handoff is None:
        print(f'[...] no handoff for {run_date_str}; nothing to size'); return 1
    signals = handoff.get('signals', [])
```
Then make the `if not signals:` bail (line ~215) EOD-aware too: in EOD mode do **not** return — let `size_positions()`→`sharpe_cadence()` self-load the APPROVED set (it handles the empty case as zero-orders + flatten-via-run_reconcile).

**Scope/safety:** contained to the driver's signal-loading preamble. Sizing math + the library are untouched (already correct). Gate-OFF (`OPENCLAW_EOD_RECONCILE` unset) must stay byte-identical — the existing ~50 sizer regression tests + an explicit legacy-path test guard this.

**New TDD test:** `main()` with `OPENCLAW_EOD_RECONCILE=1`, no handoff present, but N `APPROVED`/`target_date=today` rows in `execution_signals` ⇒ produces a non-empty sized payload (and 0 rows ⇒ zero orders, no crash). Plus a gate-off test: legacy handoff path unchanged.

**Verify after fix:** dry-run is insufficient (it short-circuits). On a non-trading-window, set `OPENCLAW_EOD_RECONCILE=1` and run `python3 src/execution/regime_blended_sizer_live.py --date <today>` against the live APPROVED set with `--dry-run` removed but the executor gated off / pointed at paper — confirm it loads the APPROVED carried set and emits a sized payload instead of rc=1.

## FIX APPLIED + VERIFIED (2026-06-03, Wed 00:4x ET)
- **Edit (live checkout `/root/openclaw`, branch feat/sp6-phase-a-eod-open-execution):** `regime_blended_sizer_live.py:main()` now has the EOD branch — gate ON ⇒ synthesize minimal handoff (`signals=[]`, `regime={}`), no bail; gate OFF = verbatim legacy. Plus regime backfill (`handoff['regime']=regime` after DB resolution) so the persisted payload / trade report carry the real regime (only consumer of `payload['regime']` is `send_report.py:128`, report label — cosmetic, not execution). NOT yet committed; on-disk = armed for the next 3:55pm ET cron (no johnbot restart needed — `trade` is a per-cron subprocess).
- **TDD:** `tests/test_sizer_live_eod_main.py` — 3 tests (eod-reaches-sizer / gate-off-returns-1 / payload-carries-regime). Red→Green confirmed. 136 unit tests green incl. 23 existing sizer/EOD; the only failures are pre-existing `POSTGRES_URI required` DB-integration tests (run_reconcile/update_pnl_mark), unrelated.
- **Correlation retention — EMPIRICALLY CONFIRMED** (user's ask). Side-effect-free probe ran the REAL library EOD path against the live 06-02 APPROVED set (341 tradable): `orthogonalization.fold 341→339`, `orthogonalization.corr_weight deflated gate for 172 tickers`, gate dropped 158<min_cum_sharpe=4.0 (kept 14), **14/14 orders with populated block-stacked brackets, 0 close_only**. All four live gates (FOLD/CORR_WEIGHT/ORTHO_SHADOW/BRACKET_STACK) fire downstream of the loader on real data. Correlation sizing is retained by construction (it lives in `_sharpe_cadence_path` after the L511 loader branch, keyed off loader-independent `_ortho_groups`/`weight_by_strat`/`sharpe_by_strat`).
- **OPEN (operator):** arm for today's 3:55pm ET (first real into-close fill) vs stage; commit fix to branch; re-arm the fill-verify watchdog for today. Today 06-03 still has 92 COMPUTED / 0 APPROVED until the 9:15 ET gate promotes them.

## Downstream (still blocked on this)
- **B0** (fill-persistence) and **B1** (live-shadow) both read `alpaca_submissions`, which is **empty** because no EOD orders have executed. They are MOOT until this fix lands and the into-close fill actually places orders. Re-evaluate B0/B1 activation only after a real EOD fill writes `alpaca_submissions`.
- Confirm during fix: that the EOD executor (`alpaca` step) DOES write `alpaca_submissions` rows when it places orders (B0/B1 depend on it; today gave no test case since 0 orders).
