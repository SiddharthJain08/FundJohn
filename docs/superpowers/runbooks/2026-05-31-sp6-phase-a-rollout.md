# SP-6 Phase A — Rollout Runbook

**Date:** 2026-05-31
**Branch:** `feat/sp6-phase-a-impl`
**Spec:** `docs/superpowers/specs/2026-05-31-sp6-phase-a-eod-open-execution-design.md`
**Status:** Gates OFF by default; staged validation required before activation.

---

## Prerequisites (completed in this branch)

1. **t+1 backtest branch merged** — backtest fills at `close[t+1]` (not `close[t]`).
   Confirms the two-ledger parity model: strategy ledger marks entry at official
   `close[T+1]` → byte-matches the t+1 backtest.

2. **Migration 126 applied** — adds lifecycle columns to `execution_signals` plus
   the two new tables.

   ```sql
   -- Confirm from psql (read-only checks):
   \d execution_signals
   -- Expect: lifecycle_state TEXT, target_date DATE, computed_at/approved_at/
   --         executing_at/filled_at TIMESTAMPTZ, gate_verdict JSONB,
   --         fill_price NUMERIC, mark_entry_price NUMERIC

   \d signal_gate_verdicts
   -- Expect: id BIGSERIAL PK, signal_id UUID, gate_type TEXT, ticker TEXT,
   --         target_date DATE, verdict TEXT, panic_score NUMERIC, news_count INT,
   --         severity INT, model TEXT, metadata JSONB, actor TEXT, decided_at TIMESTAMPTZ

   \d eod_compute_health
   -- Expect: id BIGSERIAL PK, run_date DATE, run_at TIMESTAMPTZ, rc INT,
   --         n_strategies_ok/n_strategies_total INT, regime_ok BOOLEAN,
   --         universe_size INT, healthy BOOLEAN, detail JSONB
   ```

   Apply if not yet applied:

   ```bash
   psql "$POSTGRES_URI" -f src/database/migrations/126_sp6_overnight_signal_state.sql
   ```

---

## Architecture Recap

The SP-6 Phase A day-cycle (all times ET, Mon–Fri) is a **time-split** in two windows:

**4:15 PM (T) — EOD compute** (`OPENCLAW_EOD_SIGNAL_REGISTER`): `daily-cycle.js` runs
`[collect, sentiment, signals]` with reason `eod-signal-register` on real `close[T]` prices
(fires after the ~4:05 PM EOD price append). `engine.py:write_signals` writes
`execution_signals` rows at `lifecycle_state='COMPUTED'` with `target_date=T+1`, using
`close[T]` as the decision-price reference. Also writes an `eod_compute_health` sentinel.
No orders are submitted.

**9:15 AM (T+1) — carry-forward gate** (`OPENCLAW_EOD_PREMARKET_GATE`): reads the `COMPUTED`
set, applies panic-score + FinBERT sentiment verdict per ticker, transitions rows to
`APPROVED` or `REJECTED`, writes `signal_gate_verdicts` rows, and commits the
`__gate_ran__` sentinel in the same transaction. Fail-open: if FinBERT (:7872) is down,
the gate approves + alerts rather than vetoing everything.

**9:28 AM (T+1) — open-window reconcile** (`OPENCLAW_EOD_RECONCILE`): `open_reconcile.run_reconcile`
diffs the `APPROVED` carried set (sign-only, no sizing) against the live broker book via
`_classify_position_deltas`. It acts **only on closes**: dropped signals (`orphan_close`)
and old legs of flips (`flip_close`) are submitted as OPG orders (mode-dependent); partial
resize-downs and new opens are intentionally ignored here (they belong to the 3:55 PM sizer).
Zero `APPROVED` + non-empty book = FLATTEN, but only when `eod_compute_health.healthy=True`
AND the `__gate_ran__` sentinel is present; if either is missing the reconcile refuses to
flatten, preserves all positions, and fires a loud alert.

**9:32 AM (T+1) — post-open day-sweep** (stub, `OPENCLAW_EOD_RECONCILE`): invokes
`open_reconcile.py --sweep`; in Phase A this is a no-op stub (see Known Limitations).
Unfilled OPG drops from 9:28 are handled in-loop (see below) and by the 3:55 PM backstop.

**3:55 PM (T+1) — into-close fill** (`OPENCLAW_EOD_SIGNAL_REGISTER`): the production sizer
(`regime_blended_sizer_live`, Task 8a SP-6-aware) runs as the `trade` step, loads the
`APPROVED` carried set (bypasses cadence gate in EOD mode), sizes against current NAV/BP,
then the `alpaca` step fills new/resize entries into `close[T+1]`. By 3:55 PM the drops
are already closed, so the sizer's re-diff sees them gone (no double-close; any unfilled
paper-OPG drop is also caught here as a free backstop).

**4:15 PM (T+1) — parity mark finalized**: the T+1 EOD compute sets
`mark_entry_price = official close[T+1]` (the strategy-ledger entry mark) and
re-anchors brackets via `_reanchor_bracket` for positions filled that day → zero-width
execution ledger; strategy returns byte-match the t+1 backtest. This is the
canonical mark: the backtest fills at `close[t+1]`, the strategy ledger marks at
`close[T+1]` — they are the same price.

---

## Staged Validation (All EOD Gates Stay OFF Until Final Activation)

The `eod_gate_consistency` doctor check **fails** on any partial gate activation (some ON,
some OFF). Perform the first three validation steps as **one-off manual runs** with the
gate set only for that invocation — do not flip the persistent `.env` gates until step 4.

### Step 1 — Ship Code, Gates OFF; Verify Doctor Green

After deploying this branch (johnbot restarted with gates OFF):

```bash
# Confirm all three doctor checks pass / are in expected state
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw
python3 src/maintenance/doctor.py 2>&1 | grep -E "eod_mutual|eod_gate|FAIL|WARN|OK"

# Expected:
#   eod_mutual_exclusion  OK  active_flow=neither
#   eod_gate_consistency  OK  eod_gates=all_off
```

Confirm the existing legacy/close-exec cycle still runs normally (10:00 AM cron fires,
no SP-6 crons registered).

### Step 2 — Shadow Compute (Manual, Gates OFF in .env)

Run the 4:15 PM compute once as a one-off with `OPENCLAW_EOD_SIGNAL_REGISTER=1` set
only for this invocation. Do this on a trading day AFTER the EOD price append (~4:05 PM).

```bash
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw

OPENCLAW_EOD_SIGNAL_REGISTER=1 node bin/run-graph.js 'daily-cycle' \
  '{"runDate":"'"$(date +%Y-%m-%d)"'","reason":"eod-signal-register","requestedSteps":["collect","sentiment","signals"]}'
```

Verify (no orders should be submitted — the compute step has no `alpaca`/`trade` steps):

```bash
# Expect COMPUTED rows with target_date = next trading session
psql "$POSTGRES_URI" -c "
  SELECT lifecycle_state, target_date, COUNT(*) AS n
  FROM execution_signals
  WHERE signal_date = CURRENT_DATE
  GROUP BY lifecycle_state, target_date
  ORDER BY target_date, lifecycle_state;
"
# Expect: lifecycle_state='COMPUTED', target_date = tomorrow (or next Mon if Friday)

# Expect an eod_compute_health row for today
psql "$POSTGRES_URI" -c "
  SELECT run_date, healthy, n_strategies_ok, n_strategies_total, regime_ok
  FROM eod_compute_health
  ORDER BY run_date DESC LIMIT 3;
"
# Expect: run_date=today, healthy=true (unless regime or strategy errors)

# Confirm zero orders were submitted
psql "$POSTGRES_URI" -c "
  SELECT COUNT(*) FROM alpaca_submissions
  WHERE submitted_at >= NOW() - INTERVAL '10 minutes';
"
# Expect: 0
```

### Step 3 — Dry-Run Reconcile (Manual, Gate Set Only for This Invocation)

Simulate the 9:28 AM reconcile against the live book without submitting anything.
Run this on the morning after a shadow-compute (so APPROVED rows exist; run the
premarket gate manually first if needed, or note that dry-run works on COMPUTED rows too).

```bash
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw

OPENCLAW_EOD_RECONCILE=1 python3 src/execution/open_reconcile.py --dry-run
```

Verify:
- Log lines show planned `CLOSE` / `FLATTEN` actions for each dropped ticker.
- No orders appear in `alpaca_submissions`.
- No `execution_signals` rows transition to `CLOSED_AT_OPEN`.
- If `eod_compute_health` is missing or unhealthy AND the target set is empty, expect
  a "REFUSING FLATTEN" log line (correct behavior).

### Step 4 — OPG Paper Spike (Manual, Small)

Confirm `opg_then_day` closes a dropped position at the open or via the in-loop
RTH fallback. Choose a single small position that will legitimately drop from the
next APPROVED set (or manually create a test drop scenario on paper).

```bash
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw

# Set mode to opg_then_day for this invocation (paper default)
OPENCLAW_EOD_RECONCILE=1 OPENCLAW_OPEN_CLOSE_MODE=opg_then_day \
  python3 src/execution/open_reconcile.py
```

Poll to a terminal state (do NOT accept submit-ack as fill):

```bash
# Watch order status until filled / canceled / expired (not just 'accepted')
alpaca order list --status open 2>/dev/null
# After 9:30 ET open, check for filled or expired:
alpaca order list --status closed 2>/dev/null | head -20
```

Expected behavior:
- If premarket session: OPG order submitted for the first dropped ticker; execute_single
  blocks polling until terminal (filled/expired/canceled).
- First ticker takes the OPG path; subsequent tickers in the same loop iteration
  are handled via in-loop RTH `tif=day` close once the blocking poll advances past 9:30.
- After the loop, any confirmed fills should appear as `CLOSED_AT_OPEN` in `execution_signals`.
- At 3:55 PM the sizer re-diff should see those positions gone and NOT re-submit closes.

---

## Activation — Flip All Gates Together

Only flip after Steps 1–4 pass. The three EOD gates are all-or-nothing; the doctor
`eod_gate_consistency` check fails on any partial mix.

**Persistent `.env` changes:**

```bash
# In /root/openclaw/.env (VPS live env):
OPENCLAW_CLOSE_EXEC_LIVE=0          # must be 0 (mutual exclusion enforced)
OPENCLAW_EOD_SIGNAL_REGISTER=1
OPENCLAW_EOD_PREMARKET_GATE=1
OPENCLAW_EOD_RECONCILE=1
OPENCLAW_OPEN_CLOSE_MODE=opg_then_day   # paper; switch to opg_live for live-account cutover (Phase B/C)
```

**Restart johnbot:**

```bash
systemctl --user restart johnbot.service
systemctl --user status johnbot.service
```

**Verify doctor after restart:**

```bash
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw
python3 src/maintenance/doctor.py 2>&1 | grep -E "eod_mutual|eod_gate|FAIL|WARN|OK"

# Expected:
#   eod_mutual_exclusion  OK  active_flow=eod_signal_register
#   eod_gate_consistency  OK  eod_gates=all_on
```

**Verify crons are registered** (check johnbot logs after restart):

```bash
journalctl --user -u johnbot.service -n 50 | grep -E "Cron|15 16|15 9|28 9|32 9|55 15"
# Expect log lines for: EOD compute (4:15pm), premarket gate (9:15am),
#                       open-window reconcile (9:28am), post-open sweep (9:32am),
#                       into-close fill (3:55pm).
```

**Critical timing note:** the 4:15 PM compute cron must fire AFTER the EOD price-append
(~4:05 PM). The cron at `15 16 * * 1-5` fires at 4:15 PM ET — 10 minutes after the
append — which is the built-in buffer.

---

## Monitoring

Three `system_checks` in `src/system_checks/checks/pipeline.py` surface ongoing health:

| Check | Gate Required | Signals |
|---|---|---|
| `eod_compute_health_fresh` | `OPENCLAW_EOD_SIGNAL_REGISTER=1` | Latest `eod_compute_health` row is today and `healthy=True` |
| `carried_set_present` | `OPENCLAW_EOD_SIGNAL_REGISTER=1` | At least one `COMPUTED`/`APPROVED` signal for today |
| `gate_ran_today` | `OPENCLAW_EOD_PREMARKET_GATE=1` | `signal_gate_verdicts.__gate_ran__` sentinel present for today |

All three skip gracefully (SKIP) when their gate is OFF, so the legacy flow gets
zero spurious warnings. They run as part of the daily maintenance digest; query
manually with:

```bash
source /root/openclaw/.claude/worktrees/sp6-phase-a/worktree-env.sh
cd /root/openclaw
# Run all pipeline-tagged system checks (uses src/system_checks/cli.py as __main__)
PYTHONPATH=src python3 -m system_checks --tag pipeline
# Or run the three SP-6 checks individually:
PYTHONPATH=src python3 -m system_checks --check eod_compute_health_fresh --check carried_set_present --check gate_ran_today
```

Also monitor daily:
- `logs/eod_compute_*.log` — 4:15 PM compute output
- `logs/premarket_gate_*.log` — 9:15 AM gate output
- `logs/open_reconcile_*.log` — 9:28 AM reconcile output
- `logs/open_reconcile_sweep_*.log` — 9:32 AM sweep output (stub in Phase A; logs its no-op)

---

## Rollback

Unset the three EOD gates (or set to 0) and restore the close-exec gate, then restart:

```bash
# In /root/openclaw/.env:
OPENCLAW_EOD_SIGNAL_REGISTER=0
OPENCLAW_EOD_PREMARKET_GATE=0
OPENCLAW_EOD_RECONCILE=0
# OPENCLAW_OPEN_CLOSE_MODE can stay as-is (ignored when reconcile gate is OFF)
# Restore close-exec if desired:
# OPENCLAW_CLOSE_EXEC_LIVE=1

systemctl --user restart johnbot.service
```

Gate-OFF is **byte-identical** to the pre-SP6 codebase: no new crons are registered,
the sizer loads signals the legacy way, and `open_reconcile.run_reconcile` returns
immediately (gate disabled early-return). The legacy 10:00 AM cycle resumes if neither
`OPENCLAW_CLOSE_EXEC_LIVE` nor `OPENCLAW_EOD_SIGNAL_REGISTER` is ON.

---

## Known Phase A Limitations

1. **9:32 AM `--sweep` is a stub.** `open_reconcile._sweep_unfilled` logs a warning
   and performs zero broker actions. The actual handling of unfilled OPG drops is
   done in two ways:
   - **In-loop RTH fallback (primary):** within the 9:28 AM `run_reconcile` loop,
     the first dropped ticker submits an OPG order and blocks polling until terminal.
     Once the blocking poll advances the clock past 9:30, all subsequent tickers in
     the same loop iteration are handled via the in-loop RTH `tif=day` close path
     (the `execute_single` session check returns `rth` for those). This means only
     the first dropped ticker reliably submits an OPG order; the rest close at market
     via the RTH path during the same 9:28 invocation. For typical drop counts (1–3
     per cycle) this is correct and safe.
   - **3:55 PM sizer backstop (secondary):** any position that was dropped but not
     yet closed (e.g. a paper-OPG that expired) appears in the sizer's re-diff as an
     orphan and is closed there. The cancel/resubmit-tif=day OPG sweep (per-order
     parallel submit-all-then-poll) is deferred to Phase B.

2. **OPG is premarket-only.** If the 9:28 reconcile runs at/after 9:30 ET,
   `execute_single`'s session classifier returns `rth`, the OPG path is skipped, and
   the position closes via the RTH day path. This is safe but not OPG-optimal.

3. **Sequential loop for OPG orders.** Phase A submits drops sequentially (one at a
   time + blocking poll). A parallel submit-all-then-poll architecture is Phase B.

4. **paper→live OPG cutover.** Switch `OPENCLAW_OPEN_CLOSE_MODE` from `opg_then_day`
   to `opg_live` for the live account. Phase B/C validation is required before cutover.

5. **Alpha-conditioned scheduler and Hawkes signal are Phase B/C.** The 3:55 PM fill
   is a naive into-the-close fill (parity anchor, zero-width execution ledger). The
   participation curve, TCA gate, beat-close objective, and the low-dim Hawkes weight
   are not active in Phase A (`w_hawkes` defaults to 0 and lifts only under §28
   gate — Phase B/C).

6. **Fail-open promote is not in the reconcile.** If the premarket gate crashes and
   writes no `APPROVED` rows and no `__gate_ran__` sentinel, the 9:28 reconcile
   refuses to flatten and preserves all positions (it does NOT promote
   `COMPUTED→APPROVED`). The fail-open promote (loud alert + auto-approve) is a gate
   responsibility, currently covered by the gate's own crash-recovery path; if the
   gate never ran, the operator must promote manually or the position carry-forward
   is skipped until the next EOD cycle.
