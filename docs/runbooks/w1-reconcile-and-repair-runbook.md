# W1 — Ground-Truth Reconciliation & Service Repair (Runbook + Design)

**Date:** 2026-06-28
**Owner:** BotJohn (operator-gated execution)
**Scope:** Workstream 1 of the pre-live finalization program. Reconcile recorded state vs. reality, repair the 5 failed services, de-duplicate bot supervision, and safely commit uncommitted work. **No real-money / live-trading actions.**
**Status:** EXECUTED 2026-06-28 (operator-approved). See §11 Execution Results.

---

## 0. Context

This is the foundation workstream: every later audit (dashboard fidelity, sizing, research, integration) trusts that the system's running state matches what's recorded. A read-only diagnostic fan-out (8 agents, 2026-06-28) root-caused all open issues. Key correction to prior belief: **the Discord bot is supervised and auto-restarting** — the apparent "dead/unmanaged" state was a systemd scope artifact + a duplicate unit (see §5).

### Locked decisions
- Standardize the two permission-failing services to `User=root` (matches the master-data writer convention; avoids chown ping-pong).
- `options-archive`: **narrow the preflight gate** (keep the redundant archiver), do not disable.
- `sunday-research-code`: **let today's 18:00 UTC run ride** (idempotent); ship the cap-lower + watchdog fix for next Sunday.
- Supervision: **de-dup minimally** — disable the dead system unit, keep the running user unit as sole authority (zero downtime).
- WIP: **protect untracked files first**, then split-commit; hand-strip `manifest.json`.

### Risk controls (apply throughout)
- **Never** `git clean -fd` / blind `git checkout` / `git stash` — 22 untracked files (10 strategies + 2 scripts) exist in git nowhere and are unrecoverable. Securing them is step 1.
- **Never** run `systemctl start johnbot.service` (system scope) while user-unit PID holds `:3000` → EADDRINUSE flap. Stop the user unit first if ever consolidating.
- Every service fix is verified by a single controlled run + a freshness/health check before moving on.
- All master parquets are append-only — no DROP/DELETE of `data/master/*`.

---

## 1. Secure uncommitted work (FIRST — unrecoverable state)

The working tree mixes finished work with one entangled file. Split into clean commits; do **not** `git add -A`.

| Commit | Contents | Notes |
|---|---|---|
| **C1 — strategy batch** | 10 `S_*.py` + 10 `S_*.requirements.json` + `src/strategies/registry.py` (+10 `_IMPL_MAP` entries) + the 10 new `strategy_signatures.json` entries | Atomic: registry refs the `.py` files. Commits make strategies *eligible*, not trading (`manifest != trades`; engine reads registry DB status). Class names verified to match `_IMPL_MAP`. |
| **C2 — SP-7 §6** | `docs/runbooks/sp7-phase-c-runbook.md` (live-critical 3-part rollback doc) + `scripts/first_wide_cycle_watcher.py` | Watcher monitors the first **wide** EOD compute (Mon 06-29 16:15 ET) after the clamp deletion; self-removes. Arm its timer (§6) if the 06-29 run is to be auto-watched. |
| **C3 — spent artifact** | `scripts/sp7_c3_guarded_flip.py` | Already ran 06-26; kept for audit. |
| **`manifest.json`** | Hand-strip the **12 dangling `staging` stubs** (no impl files, not in `_IMPL_MAP`), then commit the remaining lifecycle output (weekly-cron `updated_at` + 2 auto-demotions) + 10 candidate entries | Unclean, not unsafe — all portions inert to the engine. |

**Verify:** `git status` clean except intended; `git log --oneline -4` shows C1–C3; boot smoke (`node -e "require('./src/strategies/registry.js')"` or the repo's registry-load check) passes.

---

## 2. `vol-indices` — permission fix

- **Cause:** unit `User=claudebot`; `data/master/vol_indices.parquet` is `root:root` → `PermissionError` on `to_parquet()` (fetch itself succeeds).
- **Fix:** edit `/etc/systemd/system/openclaw-vol-indices.service`, set `User=root` (or remove the `User=` line). `systemctl daemon-reload`.
- **Verify:** `systemctl start openclaw-vol-indices.service` → exit 0; `vol_indices.parquet` `last date` advances; `doctor.py` `cboe_vol_indices_freshness` = OK.
- **Feeds:** S-TR-01, S-TR-04, S-HV15, S-HV20 + doctor freshness.
- **Rollback:** restore `User=claudebot`, daemon-reload.
- **Latent (track separately):** `^VIX9D possibly delisted` yfinance warning → `vix9d_close` may silently stall.

## 3. `edgar-8k@24` — permission fix + hardening + backfill

- **Cause:** same `User=claudebot` vs `root:root` cache (`_sec_ticker_cik.json`); surfaced when the 7-day cache TTL lapsed ~Jun 4.
- **Fix (config):** edit `/etc/systemd/system/openclaw-edgar-8k@.service`, set `User=root`; daemon-reload.
- **Fix (hardening, TDD):** in `src/pipeline/backfillers/edgar.py` (~line 122) wrap `cache.write_text(...)` in try/except (or atomic tempfile write) so a cache-write failure logs+degrades instead of aborting the ingest. Test: simulate unwritable cache → ingest still returns the in-memory CIK dict.
- **Backfill:** one-shot `python3 -m scripts.ingest_edgar_8k --lookback-hours <gap>` for held tickers, where `<gap>` = hours from the last successful 8-K run to now (compute at execution — ~24 days ≈ 576h as of 2026-06-28). Batch via `--tickers`; `OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN=50` truncates 79→50/run, so run in batches. Recovers the ~3-week 8-K gap.
- **Verify:** next instance run exit 0; `edgar_8k_filings` row count advances for the gap window.
- **Feeds:** `market_news` (supplementary; redundant primary writer `alpaca_news.py`) + `edgar_8k_filings` → FinBERT → `ticker_sentiment_daily`.
- **Rollback:** restore `User=claudebot`; hardening is additive (safe).

## 4. `strategy-backtest-refresh` — timeout fix

- **Cause:** `TimeoutStartSec=3600` too short; 65-strategy serial walk-forward backtest on 2-core box (CPU 1h1m, SIGTERM at 1h). Not OOM (3.7G peak). Never reaches `[backfill] DONE`.
- **Fix:** edit `/etc/systemd/system/openclaw-strategy-backtest-refresh.service`: `TimeoutStartSec=14400` (4h) + `Nice=19`; daemon-reload.
- **Hardening (durable, separate):** wrapper persisting `run_id` + `--resume-run-id` on bounded retry (script supports resume; the timer currently mints a fresh uuid weekly, discarding partial work).
- **Verify:** next Sun 2026-07-05 10:00 UTC run reaches `[backfill] DONE`; confirm Tier-1 table `strategy_backtest_regimes` covers all 65 live strategies (if not, raise severity — Tier-2 fallback hit more often).
- **Feeds:** Tier-2 fallback weights for the live regime-blended sizer (degrades gracefully — orders by `run_at DESC`).
- **Rollback:** restore `TimeoutStartSec=3600`.

## 5. `sunday-research-code` — cap + watchdog (next week, not today)

- **Cause:** `--tier-a-cap=10` × serial LLM-coder+backtest jobs exceed the 4h budget (CPU only 1h24m → blocked on I/O, not compute). SIGTERM at timeout.
- **Decision:** **let today's 18:00 UTC run ride** — idempotent (coded strategies skip; today's 12 candidates persist). No same-day change.
- **Fix (for next Sunday):** edit `/etc/systemd/system/openclaw-sunday-research-code.service` first `ExecStart` → add `--tier-a-cap 3`. Plus per-candidate timeout/watchdog in `saturday_brain_finisher.js` so one hung StrategyCoder/backtest can't eat the budget (TDD). Optionally split the two `ExecStart`s into separate units (best-effort Opus review can't starve the coder). Do **not** merely raise the timeout.
- **Secondary:** have `_markRunComplete` stamp the `saturday_runs` row even on partial/capped completion so the Sunday research-audit green-light isn't false-flagged.
- **Verify:** next Sun 2026-07-05 run completes < 4h; `saturday_runs` flips to `completed`.
- **Feeds:** research origination (`manifest.json` writes, `research_candidates`, `saturday_runs`).

## 6. `options-archive` — narrow the preflight gate (keep redundancy)

- **Cause:** `ExecStartPre=doctor.py --required-only --quick --fail-only` returns exit 2 on **3 unrelated** FAILs (`aat_plus_tier`, `mastermind_calibration_brier`, `strategy_regime_params_consistency`) → archiver `ExecStart` never runs. **No data gap** — the JS collect-step (`collector.js:1078`) is the de-facto writer and keeps `options_eod.parquet` fresh.
- **Fix:** restrict `ExecStartPre` to archive-relevant checks only (e.g. `alpaca_auth`, `alpaca_clock`, `options_archive_freshness`). *Implementation note:* confirm `doctor.py`'s check-selection CLI (a `--checks`/`--only` flag) during execution; if absent, replace the preflight with a targeted invocation. Keep the archiver as a belt-and-suspenders backup.
- **Verify:** `systemctl start openclaw-options-archive.service` → reaches the `options-archive start` log line + exit 0; parquet still fresh.
- **Feeds:** `options_eod.parquet` → `engine.py` + ~18 IV/options strategies.
- **Carried forward → W7:** `aat_plus_tier` FAIL (SPY 30-DTE greeks = 0) ⇒ Alpaca **AAT Plus options tier may be inactive** — investigate under Alpaca live-readiness (also `engine.py _drop_zero_greeks` shrinks usable IV rows).

## 7. Supervision de-dup (zero downtime)

- **State:** two enabled `johnbot.service` units. **User-scope** (`/root/.config/systemd/user/johnbot.service`) runs the live bot (PID 3274404, `Restart=on-failure`, `RestartSec=10`, root linger → boot-persistent). **System-scope** (`/etc/systemd/system/johnbot.service`) dead since 05-29 but still enabled. Both enabled ⇒ reboot race for `:3000`.
- **Fix:** `systemctl disable johnbot.service` (system); optionally `systemctl mask` it. Keep the user unit as sole authority. **Do not stop or restart the running user unit.**
- **Optional hardening (applies on next natural restart only):** user unit `Restart=on-failure` → `Restart=always` (recover from clean exit-0 too); re-add the `ExecStartPre` doctor preflight the system unit had.
- **Doc reconciliation (→ W8):** update `CLAUDE.md` Bot Registry / Infrastructure to reference the **user-scope** `johnbot.service` as the supervisor.
- **Verify:** `systemctl is-enabled johnbot.service` → `disabled`/`masked`; `systemctl --user --machine root@ is-active johnbot.service` → `active`; `:3000` still served by PID 3274404.
- **Rollback:** `systemctl enable johnbot.service` (system).

---

## 8. Out of scope / carried forward
- `aat_plus_tier` (zero SPY greeks) → **W7** (Alpaca live-readiness; options data quality).
- `mastermind_calibration_brier` (0.298 ≥ 0.2) and `strategy_regime_params_consistency` (169 missing) → health items for a later workstream (surfaced via the doctor preflight; independent of W1).
- Durable hardening (edgar try/except, research watchdog, backtest resume-wrapper) — implemented here where low-risk; larger pieces may slot into their own commits.

## 9. Execution order
1. §1 secure WIP (C1–C3 + manifest strip) — protects unrecoverable state.
2. §2–§3 permission fixes (`vol-indices`, `edgar` → root) + edgar hardening + 8-K backfill.
3. §4 backtest timeout; §5 research cap (config only; watchdog as hardening).
4. §6 options-archive preflight narrowing.
5. §7 supervision de-dup.
6. §6/§3 verification against Monday 06-29 timers + the first wide EOD compute (16:15 ET); §1-C2 watcher armed if desired.

## 10. Verification summary (definition of done for W1)
- `git status` clean; all finished work committed; no untracked unrecoverable files.
- `systemctl --failed` shows zero `openclaw-*` units (after their next scheduled or manual run).
- Each repaired service produces fresh output / passes its doctor freshness check.
- Exactly one enabled `johnbot.service`; reboot race eliminated.
- A short reconciliation note of every "recorded ≠ reality" gap found (memory restart claims, scope artifact) for the W8 docs pass.

---

## 11. Execution results (2026-06-28, operator-approved)

All live `/etc/systemd/system/*` units were edited + `daemon-reload`ed; the `docs/`
source templates were synced to match; code changes committed on
`feat/intraday-regime-15min-prefetch`. All 23 untracked files were tar-backed-up
to scratchpad before any git op.

### Done & verified now
- **WIP secured** — C1 `9635ecf` (10 strategies + registry/signatures/stripped
  manifest), C2 `6379720` (§6 watcher + runbook), C3 `bfec8d7` (C3-flip artifact).
- **vol-indices** → `User=root`; ran `exit 0`, parquet advanced 06-25→06-26.
- **supervision** — dead **system** `johnbot.service` **disabled** (reboot race
  gone); **user** unit hardened (`Restart=always` + non-blocking doctor preflight
  + StartLimit backstop). Zero downtime (bot PID 3274404 untouched).
- **sunday-research-code** → `--tier-a-cap 3` (applied after the 18:00 UTC run
  started, per let-it-ride). Verifies Sun 2026-07-05.

### Done; functional re-verify on next scheduled run
- **edgar** → `User=root` + non-fatal `_write_cik_cache` (C6 `2a73613`, 2 tests).
  Mon 07:15 ET. 3-week 8-K backfill = separate Monday one-shot (trading-day-gated).
- **options-archive** → `User=root` + narrowed preflight via new `doctor --only`
  (C5 `bc921f0`, 3 tests; preflight verified `exit 0`). Mon 16:30 ET.
- **strategy-backtest-refresh** → 4h timeout + `Nice=19` (C4 `9f86fe9`). Sun 07-05.
- **weekend-saturday** (6th unit, found via `systemctl --failed`) — step 5
  (`unified_backtest --all-live`) is now bounded to 6h + niced inside
  `refresh_backtests.sh`, and WARN-continues instead of hard-aborting, so steps
  6-8 (weights/panels/universe) always run on last-known backtests. Unit
  `Nice=19` + `TimeoutStartSec` 8h→10h. The standalone `openclaw-backtest-refresh.timer`
  is disabled (no double-run). **Minimal mitigation only** — backtest refresh may
  stay incomplete until the durable per-strategy-subprocess/watchdog redesign.
  Verifies Sat 2026-07-04.

### Carried forward
- **Alpaca AAT Plus tier** (zero SPY greeks; doctor `alpaca_aat_plus_tier`) → W7.
- **7 pre-existing broken tests** (`test_doctor_mastermind_addendum_health` expects
  removed `_query_addenda_health`; `test_doctor_universe_recs` references a deleted
  worktree) — proven NOT W1-caused (fail on pre-W1 doctor.py) → test-hygiene workstream.
- **Durable hardening**: edgar backfill (Mon), research per-candidate watchdog,
  backtest-refresh resume-wrapper, weekend-saturday per-strategy redesign.
- **Architecture (W8)**: satellite units use `PartOf=johnbot.service` (system, now
  disabled) while the bot runs user-scope — reconcile; `docs/strategy-backtest-refresh.service`
  is a stale duplicate to dedupe.
- **Recorded≠reality reconciled**: memory's "johnbot restarted (PID 3274404)" was
  TRUE (user-scope unit); the "dead a month / unmanaged" reading was a systemd-scope artifact.

### Observability (C8 — no-silent-failure close)
- **weekend-saturday** now exits non-zero when step 5's refresh was incomplete
  (after steps 6-8 run), so the unit fails and alerts instead of silently feeding
  the sizer stale backtests. **Expect a weekly `failed` + #botjohn-log alert until
  the per-strategy redesign** — that is the honest incomplete-refresh signal, not a
  regression. The 07-04 run also answers: does the capped refresh rotate coverage
  or skip the SAME strategy tail? (the latter ⇒ prioritize the redesign).
- `OnFailure=openclaw-failure-notify@%n.service` added to vol-indices,
  options-archive, weekend-saturday (were missing → silent for weeks). edgar@
  left without it (templated-unit double-`@` finickiness); it stays visible via
  `systemctl --failed` + the Monday check.
- **Open**: confirm the disabled standalone `openclaw-backtest-refresh.timer` is
  intentional (if so, capped step 5 is the ONLY place `--all-live` runs → staleness
  is systemic until redesign). A scheduled "did each repaired unit's next run pass?"
  check is still owed for the Mon/Sat/Sun dated verifications.

### Final state
- `systemctl --failed` (openclaw): **0 units**.
- W1 commits C1–C8 on `feat/intraday-regime-15min-prefetch`.
- During execution the live research finisher (PID 3335484) was actively coding
  candidates — research origination pipeline confirmed functional (preview of W4).
