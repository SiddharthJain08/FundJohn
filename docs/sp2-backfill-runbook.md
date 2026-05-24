# SP-2 Phase B — Backfill Runbook

## Overview

Phase B ships the one-shot 5-year historical backfill of `ticker_metadata_snapshots`
plus narrow gap-fills of `prices.parquet` and `options_eod.parquet`. It runs
operator-invoked from the command line — not on a timer. The driver
(`scripts/backfill_universe_5y.py`) implements a **stage → validate → promote**
loop with per-chunk Redis checkpointing and a durable
`backfill_audit` row (migration 115) for every chunk attempted.

The system's append-only invariant on `data/master/` is preserved verbatim
EXCEPT for one narrowly-gated, audit-logged path: `_promote_chunk` in
`scripts/backfill_universe_5y.py` may overwrite an existing `(symbol, date)`
row IF:

1. The `_existing_dates_for` precondition reports zero overlap (default
   `--source-tag backfill_5y_v1`), OR
2. The operator has set `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`, used a
   `--source-tag backfill_5y_vN` (N > 1), AND paired the overwrite with a
   `data_quarantine` supersede row (the `--supersede-quarantine` flag).

This is the SOLE permitted exception — see
[`feedback_never_delete_master_data.md`](../../.claude/projects/-root/memory/feedback_never_delete_master_data.md).

Rows that slip past validation are recovered at READ time via
`src/pipeline/quarantine_filter.py`, which is wired into the resolver,
three backtest engines, and the collector startup hook. Three other
backtest engines (`regime_blended`, `intraday_regime`,
`regime_performance_analyzer`) read parquets that the filter does not
cover (`historical_regimes.parquet`, `ts_utc`-keyed bars, or no parquet
reads at all) and are documented as unfilterable in
`src/pipeline/quarantine_filter.py`.

## Pre-flight checklist

> **Phase B doctor + system_checks gate.** The new
> `backfill_progress` / `backfill_universe_coverage` doctor checks AND
> their `system_checks` peers (`backfill_progress`,
> `ticker_metadata_history_depth`) are gated on
> `OPENCLAW_BACKFILL_5Y_ACTIVE=1`. **Set this only AFTER you have run
> the backfill at least once and the audit table reflects production
> state, otherwise the checks will surface test residue (currently
> ≈4162 quarantined rows from Tasks 7-9 smoke passes) as alarms** and
> the daily 10am cron will abort on preflight. Default-OFF preserves
> the pre-Phase-B behavior; the operator flips it as part of the
> "production activation" step after the first real backfill drains
> the test-tag rows.

Run `bash scripts/preflight_phase_b.sh` (committed in `14fa573`). It exits
0 if every gate passes, non-zero with explanation on first failure. It
performs zero state mutations.

The 11 gates:

1. **Phase A live** — `OPENCLAW_UNIVERSE_RESOLVER=1` in `.env` and
   `ticker_metadata_snapshots` has at least one row.
2. **Disk free ≥ 40 GB** under `/root/openclaw/data/` (backfill projection
   ≈ 30 GB across targets).
3. **Redis reachable** — `redis-cli PING` returns `PONG`.
4. **Alpaca tier** — `alpaca account info` reports `algo_trader_plus`.
5. **FMP day-quota headroom** — `data_provider_health` shows current
   usage < 50% of 250k/day (WARN-only above that threshold).
6. **Backfill universe artifact** — `data/.backfill_universe_v1.txt` exists
   and is committed.
7. **SP500 historical membership** — `data/sp500_historical_membership_v1.csv`
   exists and is committed.
8. **Migration 115** — `backfill_audit` table present.
9. **Doctor preflight green** — `python3 -m src.maintenance.doctor` returns 0.
10. **Discord webhook** — `DISCORD_BACKFILL_LOG_WEBHOOK` set in `.env`
    (warn-only, not fatal).
11. **Dry-run smoke** — 5-ticker × 1-year `--dry-run` completes cleanly.

## Kickoff commands

Per-target invocation. **Sequence matters:** prices must complete before
metadata, because `build_month_snapshot` derives `adv_usd_20d` (and the
`in_r1000`/`in_r3000` ranks when fallback ships) from `prices.parquet`.

```bash
# Step 1: prices (longest; expect 3-5 days wall on full universe)
nohup python3 scripts/backfill_universe_5y.py --target prices --resume \
  > /var/log/backfill_prices.log 2>&1 &

# Step 2 (after prices completes): metadata
nohup python3 scripts/backfill_universe_5y.py --target metadata --resume \
  > /var/log/backfill_metadata.log 2>&1 &

# Step 3 (optional, narrow window): options gap-fill
nohup python3 scripts/backfill_universe_5y.py --target options \
  --start-date 2026-05-10 --end-date 2026-05-22 --resume \
  > /var/log/backfill_options.log 2>&1 &
```

`--resume` is safe to pass on the first run — it is a no-op when no
Redis checkpoint exists. Always pass it so a Redis flush or driver
restart does not redo completed chunks.

## Monitoring

- **Discord** — `#backfill-log` channel (webhook `DISCORD_BACKFILL_LOG_WEBHOOK`)
  receives per-chunk status and a daily digest.
- **Operator dashboard** (`:7870`) — `/api/backfill-progress` tile shows a
  30-second SSE stream of `(in_progress / validated / promoted / quarantined)`
  counts per target.
- **User dashboard** (`:3000` Data Health tab) — `/api/pipelines/backfill-history`
  panel renders a monthly timeline of `ticker_metadata_snapshots` row counts
  (target ~3000/month at full coverage).
- **Doctor** — `_check_backfill_progress` and `_check_backfill_universe_coverage`
  (both `slow=True`) reflect chunk distribution and per-month row-count gates.
- **system_checks** — `backfill_progress` (storage tag) and
  `ticker_metadata_history_depth` (strategies tag) surface in the daily
  maintenance digest.
- **Audit log** — every chunk attempt has a row in `backfill_audit` (mig
  115). Survives `FLUSHDB` on the `backfill:` Redis namespace.

## Known limitations (Phase B v1)

- **Historical `market_cap = None`.** FMP Starter tier returns 403 on
  the historical-market-capitalization endpoint. The fallback path
  (`market_cap = price × shares_outstanding`) requires a
  `shares_outstanding` backfill that is **not** shipped in Phase B.
  Consequence: `in_r1000` / `in_r3000` are False for every historical
  snapshot until shares-outstanding backfill ships. The daily writer
  (today's row only) is unaffected — it reads current `market_cap`
  from the live FMP profile endpoint.
- **Backfill universe seed = 404 tickers, not ~3000.** The frozen
  artifact `data/.backfill_universe_v1.txt` is intersected with tickers
  that have a row in the current `prices.parquet`, which today covers
  only ~454 names. Once Task 7 broadens prices coverage, the operator
  re-runs `python3 scripts/build_backfill_universe.py --force` to write
  a v2 artifact and re-commits it.
- **`AAPL` / `MSFT` `first_seen_at = 2026-05-14`.** Phase A's
  `alpaca_tradable_universe` tracking only started 2026-05-14, so any
  historical metadata snapshot whose ticker lacks a tracked listing
  date is excluded. The frozen membership CSV
  (`data/sp500_historical_membership_v1.csv`) covers SP500 specifically;
  non-SP500 tickers carry no historical listing date.
- **`backfill_audit` test residue** ≈ 4162 rows from Tasks 7-9 smoke
  passes. The `backfill_progress` `system_check` WARNs on this residue.
  Acceptable pre-deploy; clear either by inserting production rows that
  surge the `promoted` count above the WARN threshold, or treat the
  count as a known baseline and re-evaluate after the first real run.

## Quarantine handling

A row reaches `data_quarantine` either when validation flags it during
backfill (schema mismatch, row-count plausibility, > 0.1% live delta)
OR when the operator manually inserts a row after spotting a downstream
anomaly. In both cases the master parquet row stays in place — recovery
is read-time only.

Manual insertion pattern:

```sql
INSERT INTO data_quarantine
  (master_table, symbol, affected_date, source_tag, reason, flagged_by)
VALUES
  ('prices.parquet', 'XYZ', '2022-08-15', 'backfill_5y_v1',
   'close price 12x reasonable', 'operator:siddharth');
```

Recovery sequence (per spec §2.3 and §6.2):

```bash
OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 \
  python3 scripts/backfill_universe_5y.py \
    --target prices --tickers XYZ --years 2022 \
    --source-tag backfill_5y_v2 --supersede-quarantine
```

The promote step writes a corrected row at the same `(symbol, date)`
under the v2 source_tag; `data_quarantine.superseded_by_source_tag` is
updated to `backfill_5y_v2` and `superseded_at = NOW()`.

## Rollback ladder

From spec §6.2:

- **Level 1 — Halt the backfill, no data changes.** `kill <pid>`. Redis
  chunks freeze in their current state. Reversible in ≤ 5 s. Live system
  is unaffected because backfill is operator-invoked, not on a timer.
- **Level 2 — Quarantine identified bad rows.** Insert `data_quarantine`
  row(s) and run the recovery sequence above. Wall: minutes per
  `(symbol, date)`.
- **Level 3 — Disable consumer reads of backfilled data.** Set
  `OPENCLAW_BACKFILL_5Y_ACTIVE=0` plus the companion
  `OPENCLAW_PARQUET_FILTER_BACKFILL_ROWS=1`. The quarantine filter then
  treats every row tagged `backfill_5y_%` as quarantined at read time.
  System reverts to pre-Phase-B coverage (SP500 prices + post-Phase-A
  live snapshots only). Wall: ≤ 30 s (env flip + restart).
- **Level 4 — Full rebuild from scratch.** `DELETE FROM backfill_audit`,
  `FLUSHDB` on the `backfill:` Redis namespace, then re-run the driver
  with `--source-tag backfill_5y_v2` and
  `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`. Wall: full 5-7 days as the
  initial run.

## Operator-only operations

- `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1` — unlocks v2+ `source_tag`
  overwrites in `_promote_chunk`. Required for any quarantine recovery.
- `--supersede-quarantine` — pairs the overwrite with a
  `data_quarantine.superseded_by_source_tag` update so the audit trail
  reflects the recovery.
- Direct `data_quarantine` inserts — for post-validation poisoning
  spotted downstream (Level 4 path). Always include `flagged_by` and a
  `reason` string future-you can act on.
