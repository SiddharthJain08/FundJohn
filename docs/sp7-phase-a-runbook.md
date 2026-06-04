# SP-7 Phase A — Operator Runbook

## Merge + flip (one sitting)
1. Merge `feat/sp7-phase-a-data-foundations` into the live branch (`--no-ff`).
   NEVER `git reset --hard` on the live checkout (uncommitted live-critical
   files: manifest.json, strategy_signatures.json, run_sentiment_step.py).
2. Migration 129 (listed_date) is ALREADY APPLIED live (Task 4 smoke).
   Verify: `docker exec openclaw-postgres psql -U openclaw -d openclaw -t -c
   "SELECT count(*) FROM information_schema.columns WHERE
   table_name='alpaca_tradable_universe' AND column_name='listed_date';"` → 1
3. Add to /root/openclaw/.env:  OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500
4. Restart johnbot:  XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service
5. Start the split-watcher timer (installed enabled-but-not-started):
   XDG_RUNTIME_DIR=/run/user/0 systemctl --user start sp7-split-watcher.timer
6. Verify the next engine run logs "universe clamp 'sp500': kept ..., dropped 29"
   (engine spawns per-cycle; no further restarts needed). The kept count must
   stay ~586 throughout the backfill (acceptance: clamp parity).

## One-time data jobs (any order, nice -19, after merge)
7. EDGAR shares for the v2 universe (~4.5k, ~70 min at 8 req/s):
   cd /root/openclaw && set -a && . <(grep -E '^POSTGRES_URI=' .env) && set +a
   nice -n 19 python3 -m src.pipeline.backfillers.edgar_shares --universe-file data/.backfill_universe_v2.txt
8. listed_date probe (full NULL sweep, ~25-40 min for ~13.9k):
   nice -n 19 python3 scripts/probe_listed_dates.py

## The 4.5k backfill (multi-night, ONLY after steps 3-6 verified)
9. Start the overnight timer + arm:
   XDG_RUNTIME_DIR=/run/user/0 systemctl --user start sp7-overnight-backfill.timer
   touch /root/openclaw/data/.sp7_backfill_armed
10. Watch: logs/sp7_backfill_<date>.log; window = 01:00→13:00 UTC, Mon-Fri
    nights only (Saturday excluded — weekend-refresh stack owns the box).
    Expect ~3-5 nights. Pre-listing year-chunks quarantine 'empty DataFrame'
    — expected and benign (GEV-pattern).
11. On COMPLETE (wrapper disarms itself; "[sp7-backfill] COMPLETE" in log):
    a. python3 scripts/activate_universe_v2.py
    b. Historical metadata for v2 names — checkpoint gotcha: month-chunks are
       Redis-marked from earlier runs ('promoted' or 'quarantined'); BOTH skip.
       Run WITH supersede (metadata insert is ON CONFLICT DO NOTHING —
       append-only safe):
       OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 nice -n 19 python3 scripts/backfill_universe_5y.py \
         --target metadata --source-tag backfill_5y_v2 --supersede-quarantine \
         --tickers "$(paste -sd, data/.backfill_universe_v2.txt)"
    c. Acceptance checks (below).

## Acceptance
- prices.parquet ≈ 5,100+ distinct tickers; v2 coverage complete:
    nice -n 19 python3 - <<'EOF'
    import pandas as pd
    cov=set(pd.read_parquet('data/master/prices.parquet',columns=['ticker']).ticker.unique())
    v2=[l.strip() for l in open('data/.backfill_universe_v2.txt') if l.strip()]
    missing=[t for t in v2 if t not in cov]
    print(len(cov), 'tickers | v2 missing:', len(missing), missing[:10])
    EOF
    (a handful of never-listed/genuinely-empty names is acceptable — cross-check
    backfill_audit 'empty DataFrame' quarantines)
- Latest snapshot: market_cap ≥95% of v2∩CIK-mapped; r1000 = 1000; r3000 = 3000:
    SELECT count(*) FILTER (WHERE market_cap IS NOT NULL) AS caps,
           count(*) FILTER (WHERE in_r1000) AS r1000,
           count(*) FILTER (WHERE in_r3000) AS r3000
    FROM ticker_metadata_snapshots
    WHERE snapshot_date=(SELECT max(snapshot_date) FROM ticker_metadata_snapshots);
    (r1000/r3000 reach their full 1000/3000 only once caps cover the breadth)
- Engine clamp held: grep latest cycle log for "universe clamp 'sp500'" —
  kept count must NOT have grown with the backfill.
- Phase B (tier-ladder universe backtest) is GO when all three pass.

## Abort / rollback
- Pause backfill: rm /root/openclaw/data/.sp7_backfill_armed (promoted chunks
  stay — append-only; harmless behind the clamp). Stop timer:
  XDG_RUNTIME_DIR=/run/user/0 systemctl --user stop sp7-overnight-backfill.timer
- Clamp off: remove the env line + johnbot restart (returns engine to
  all-parquet behavior).
- Adjustment convention rollback: revert the collector.js hunk (single line) —
  but note backfilled history is split-adjusted regardless.

## Known follow-ups (non-blocking)
- alpaca_options.py `_append_parquet` writes the OPTIONS master non-atomically
  (pre-existing, lock-protected, never TERM-killed today). Give it the same
  tmp+os.replace treatment when next touched.
- GOOG/GOOGL share an entity-level cap (EDGAR companyfacts) — both rank by
  total Alphabet cap. Acceptable for tier ranking; revisit if per-class
  precision ever matters.
- Daily EDGAR shares refresh cadence: shares update quarterly; the daily
  writer carries forward. A monthly --covered-only re-run keeps caps fresh
  (fold into Phase B recompute or a monthly timer — Phase B decision).
