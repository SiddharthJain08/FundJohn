## System Maintenance Sweep — 2026-04-25 (Saturday)

### Health snapshot — services, schedulers, services running OK
- `johnbot.service` — up (port 3000 → nginx :80, dashboard + API + Discord bridge)
- `fundjohn-dashboard.service` — up (:7870 control room)
- `mastermind-chat.service` — up (:7871 Opus 4.7 chat backing Research tab)
- All weekly Mastermind timers scheduled today/tomorrow:
  - `openclaw-mastermind-corpus.timer` Sat 14:00 UTC (corpus rater)
  - `openclaw-strategy-review.timer` Sat 22:00 UTC (comprehensive-review)
  - `openclaw-position-recs.timer` Sat 23:00 UTC (sizing recs)
  - `openclaw-paper-expansion.timer` Sun 12:00 UTC (paper-hunt)
- Migrations through `053_performance_outliers.sql` applied on every johnbot start.
- No errors in the last 24h on any of the three long-running services.

---

### Issues found, ranked by impact

#### 🔴 P1 — Master price data has a 4-day equity gap (root cause: coverage-update-on-empty-fetch + 10am-ET cycle timing)

**Symptom (visible to user)**
- Dashboard market data freshness: most recent equity bar in `data/master/prices.parquet` is **2026-04-22**.
- 04-21 and 04-23 contain only 66/440 tickers (yfinance market indices/ETFs/crypto/currencies) — every one of the 374 Polygon-source equity rows is missing.
- 04-24 (Friday) is missing entirely.
- options_eod last bar is 2026-04-21 (3 days stale).

**Mechanism (verified)**
1. The collector runs at 14:00 UTC = 10am ET. At that hour, today's EOD bars don't exist yet (markets close at 4pm ET / 20:00 UTC).
2. `getGapSummary` flags a ticker as "covered" when `data_coverage.date_to >= yesterday`. On day T's cycle, yesterday=T-1, so a ticker whose stored `date_to=T-1` is ✅ — the cycle skips fetch entirely.
3. When fetch DOES run, `runHistoricalPrices` calls `fillPricesYFinance(ticker, gap.from, gap.to)` and then `store.updateCoverage(..., gap.from, gap.to, written)` **regardless of whether `written > 0`**. So when yfinance returns 0 rows (because EOD isn't published yet), coverage advances to `gap.to` anyway — silently.
4. Compounding: yfinance's `tk.history(start, end)` is end-EXCLUSIVE. Confirmed live: `start=04-21, end=04-22` returns just 04-21. So even when fetch succeeds for the requested range, the most-recent day is dropped at the API layer.

**Result**: `data_coverage` claims AAPL is covered through 04-23, but the parquet only has rows through 04-22. Subsequent cycles trust the (lying) coverage and never refetch.

**Self-heal expectation**: Monday 04-27's cycle will see yesterday=04-26 (Sun), gap=04-24→04-26. yfinance will return Friday 04-24 only. Coverage advances to 04-26, parquet gains the 04-24 bar. Tuesday 04-28 cycle catches 04-27 if Polygon publishes. So the *current* gap will close on Monday — but the underlying bug recurs every cycle and will reopen the gap each subsequent week.

#### 🟠 P2 — TradeJohn budget bump deployed but first 04-24 attempt cost $1.66 (over the old $1.50 cap) before the bump landed
- Commit `a4b94c6` raised `maxBudgetUsd` from 1.5 → 2.5 at 18:36 UTC on 04-24, after the first attempt at 18:02 had already hit the old cap and produced no usable output (`error_max_budget_usd`, $1.66 burned, 64K output tokens).
- The third attempt (after the bump) succeeded at $1.24 in 6 min — well within the new $2.5 cap.
- Current state: no further action needed; budget is now correctly set.

#### 🟠 P3 — 2 of 10 Alpaca orders rejected on 04-24 with `stop_loss must be <= base_price - 0.01`
- CMCSA: TradeJohn stop=$28.93, Alpaca base_price=$28.275 (current quote moved below stop).
- CHTR: TradeJohn stop=$236.40, Alpaca base_price=$185.535.
- Cause: TradeJohn's stop is computed off the signal-time entry price; by the time the Alpaca submission runs, the bid/ask has moved enough that the bracket math is invalid for Alpaca's pre-trade check. Submission silently skips these; `_alert_partial` posts the partial-fill warning to #trade-reports.

#### 🟡 P4 — 04-24 sized handoff on disk has 1 order; alpaca submitted 10
- File: `output/handoffs/2026-04-24_sized.json` (mtime 20:21 UTC) contains one USO/S5_max_pain order. Alpaca log at 17:48 submitted 8 orders matching TradeJohn's intended 10-order set (BRK-B, MDLZ, CMG, LKQ, ABT, IP, MDT, TMUS, plus skips for CMCSA/CHTR).
- The pipeline log shows three `Pipeline starting` lines for 04-24 (14:00 timeout, 17:37 retry-superseded, 17:41 successful end-to-end). Two more TradeJohn invocations happened later (18:02 budget-fail, 18:33 timeout, 18:36 success @ 20:21 file write).
- The 17:41 run wrote the 10-order sized.json that Alpaca consumed; a later 18:36 manual rerun (likely operator-driven, since cron only fires once at 14:00) overwrote it with a fresh 1-order payload. **No production loss** — Alpaca already submitted the 10-order set hours earlier; the on-disk file just no longer matches what was sent. But it does mean post-hoc reconciliation on this date has to read the alpaca log, not the sized handoff.

#### 🟡 P5 — `performance_outliers` has rows only for 04-23 (100 rows); 04-24 cycle wrote zero
- `_write_performance_outliers()` is called from `trade_handoff_builder.build()` after `load_yesterdays_performance_outliers()` returns the σ-gated lists. The 04-24 cycle ran the handoff builder successfully (per log: "structured handoff written — 608 signals, 117.1 KB"), but no outlier rows landed.
- Likely cause: `load_yesterdays_performance_outliers()` reads from the prior day's `signal_pnl` joined to the prior day's structured handoff. On 04-24 cycle, "yesterday" = 04-23 — and that's the same day the 100 outlier rows already exist for, so the writeback may have been skipped by the UNIQUE constraint (`UNIQUE (cycle_date, strategy_id, ticker, kind)` from migration 053). Re-running the same date is correctly idempotent. The actual gap is that no NEW outliers were detected on 04-23's data — that's the expected behaviour; no action needed.

---

### Elementary fixes I did NOT execute (require operator green-light)

I deliberately did not mutate the master parquet, the data_coverage table, or live trading state on a Saturday during a request from you. Each is a one-line trigger when you're ready:

- **Backfill 04-23 + 04-24 equity prices** — open a Python REPL, hit yfinance for the 374 missing tickers with `start='2026-04-23', end='2026-04-25'`, append to `prices.parquet`. Reset `data_coverage.date_to` to actual max(date) per ticker after the write.
- **Reset stale coverage rows** — single SQL: `UPDATE data_coverage c SET date_to = (SELECT MAX(p.date) FROM prices p WHERE p.ticker = c.ticker) WHERE c.data_type='prices'`. Stops the cascade until the underlying P1 bug is patched.

Either is safe to run; I just want explicit go-ahead before touching production data.

---

### Detailed proposals (complex changes — for later approval)

#### Proposal A — Fix the coverage-update-on-empty-fetch bug *(P1 root cause)*

**File**: `src/pipeline/store.js`, `updateCoverage()` at line 280.

**Change**: only advance `date_to` when `rowsAdded > 0`, AND treat a yfinance call that returns no bars as the cycle saying "the data isn't available yet" rather than "we covered this range." Two-line fix:

```javascript
// store.js
async function updateCoverage(ticker, dataType, dateFrom, dateTo, rowsAdded = 0) {
  if (rowsAdded === 0) return;  // ← ADD THIS LINE — don't lie about coverage
  ...existing ON CONFLICT logic...
}
```

**Caveat**: this means tickers that genuinely have no data (USDCNH=X) will retry every cycle. Mitigation: track `attempted_at` separately from `date_to` and only re-attempt after a 24h cooldown.

**Risk**: low. The current behaviour is silently wrong; the proposed behaviour is loud-and-eventually-correct.

---

#### Proposal B — Add an evening EOD-bar refresh job *(closes the gap permanently)*

A second daily timer at **20:30 UTC (4:30pm ET)**, 30 min after market close, that runs the collector with **prices-only, force-fetch** semantics. Polygon's daily aggregates are reliably published by then.

```ini
# /etc/systemd/system/openclaw-eod-refresh.timer
[Timer]
OnCalendar=Mon..Fri 20:30 UTC
Persistent=true
Unit=openclaw-eod-refresh.service

# /etc/systemd/system/openclaw-eod-refresh.service
[Service]
ExecStart=/usr/bin/node /root/openclaw/src/pipeline/run_collector_once.js --eod-only --force
WorkingDirectory=/root/openclaw
Environment=PIPELINE_PRICES_ONLY=1
```

`run_collector_once.js` would gain a `--eod-only` flag that:
- Skips options/fundamentals/news/insider — only `runHistoricalPrices` and `runMarketPricesYFinance`.
- Sets `gap.to = today` and **does not** check `data_coverage` for the same-day check (force-fetch).
- Triggers TradeJohn / engine? No — pipeline_orchestrator is **not** invoked. The morning cycle owns trading; the evening cycle owns data hygiene only.

**Runtime estimate**: ~3 min (the equity batch is the only real work).
**Risk**: low. Adds a refresh, doesn't change trading. Idempotent because UNIQUE constraints in the parquet writer dedupe.

---

#### Proposal C — Pre-flight stop_loss validation in `alpaca_executor` *(P3)*

Before submitting each bracket, fetch the latest quote (`/v2/stocks/{ticker}/quotes/latest`) and verify `stop < bid - 0.01` for longs / `stop > ask + 0.01` for shorts. If invalid, **adjust** the stop to `bid - 0.01 - 1bp` (longs) or `ask + 0.01 + 1bp` (shorts) and log the adjustment, rather than failing the submission.

```python
# alpaca_executor.py (sketch)
def _adjust_stop_for_market(sess, ticker, direction, stop):
    q = sess.get(f'{sess._base.replace("/v2","")}/v2/stocks/{ticker}/quotes/latest', timeout=5).json()
    bid, ask = float(q['quote']['bp']), float(q['quote']['ap'])
    if direction == 'long' and stop >= bid - 0.01:
        new_stop = bid - 0.02
        log(f'Stop adjusted on {ticker}: {stop} → {new_stop} (bid={bid})')
        return new_stop
    if direction == 'short' and stop <= ask + 0.01:
        new_stop = ask + 0.02
        log(f'Stop adjusted on {ticker}: {stop} → {new_stop} (ask={ask})')
        return new_stop
    return stop
```

**Risk**: low. The adjusted stop is always closer to current price (smaller risk per share, slightly larger position dollar-loss IF we hit it before retracing). If you'd rather skip than adjust, the alternative is to pass a `_skip_invalid_brackets=true` config flag — currently the behaviour anyway.

**Scope**: 8 of 8 valid orders submitted on 04-24, so this pulls 2 more orders into the live book per cycle on average — meaningful for a small portfolio.

---

#### Proposal D — Pipeline orchestrator idempotency lock *(P4 root cause)*

`pipeline_orchestrator.py` already has a `pipeline:completed:{date}` Redis key (per CLAUDE.md). Verify it's actually being checked at the start of every invocation and not just at the end. If not, add an explicit check:

```python
if redis.exists(f'pipeline:completed:{run_date}'):
    log(f'Pipeline already completed for {run_date} — skipping')
    sys.exit(0)
```

This prevents the cascade where a manual rerun overwrites the sized handoff after Alpaca has already submitted the original. Keeping the on-disk artifact in sync with what was actually sent matters for post-hoc audit.

**Override**: `--force` flag for intentional reruns.

---

#### Proposal E — TradeJohn handoff size sanity-check before LLM invocation

Right now the structured handoff size depends on:
- # of green signals (87 today, 188 last cycle when σ_gate=2 — 2× variance day-over-day),
- # of yesterday's overperformers / underperformers attached as `d1` fields,
- the prefiltered list (folded later into vetoed by trade_agent_llm, but also lives in structured for Discord).

Above ~150KB the prompt cache prefix overflows and Sonnet 4.6 starts blowing through cache_creation tokens — that's exactly the failure mode the 04-24 first-attempt $1.66 burn maps to.

**Fix**: add a hard cap `MAX_STRUCTURED_BYTES = 100000` to `trade_handoff_builder.py::build()`. If the JSON would exceed it, log a warning and *additionally* pre-truncate the lowest-EV signals on the green list (not the prefiltered/vetoed). This reframes the problem from "LLM hits budget" to "we ship what fits and tell you what we dropped."

**Risk**: medium. Need to ensure dropped signals are still surfaced in #trade-signals so operator knows the cap was hit.

---

### Dashboard sweep — clean

I checked the dashboard surface area I most recently touched. No regressions detected:

- ARR / ADR / ACT columns rendering with the right formulas (verified via `/api/strategies` payload).
- Sortable headers respond correctly across status, ARR, ADR, ACT.
- Active Stack collapse / expand state persistent.
- Portfolio AAR (Predicted | Lifetime) renders identical pre-30-days, will diverge after enough equity history.
- P&L bar chart uses `barPercentage:1.0` & `categoryPercentage:1.0`; bars touch as designed.
- /sigma-gate Discord command still wired (verified in `bot.js` + `setup.js` server-map).

User's "market data is up to April 23rd" complaint is fully explained by P1 above — it's a data-pipeline issue, not a dashboard rendering issue. The dashboard is showing what's in `prices.parquet`; the parquet is the gap.

---

### Summary triage

| Issue | Severity | Auto-heals? | Suggested action |
|---|---|---|---|
| P1 prices.parquet 4-day equity gap | High | Partially Monday | Run backfill (one-shot) + Proposals A & B |
| P2 TradeJohn budget cap | Medium | Already fixed (commit `a4b94c6`) | None |
| P3 Alpaca stop_loss rejects | Medium | No | Proposal C |
| P4 sized handoff inconsistent with submitted orders | Low | No | Proposal D |
| P5 performance_outliers 04-24 silence | Low | Yes — UNIQUE constraint working | None |

Awaiting your green-light on the backfill and on which proposals to implement.
