# SP-7 Phase C — Live Wiring Activation Runbook

Operator-gated. Every gate is default-OFF; merge changes nothing behavioral.
Spec: docs/superpowers/specs/2026-06-07-sp7-phase-c-live-wiring-design.md

## 0. GATES INTRODUCED (all default-OFF)
| Gate | Effect when =1 |
|---|---|
| OPENCLAW_LIVE_UNIVERSE_SHADOW | signals step writes universe_shadow_parity diff rows (non-fatal sidecar) |
| OPENCLAW_LIVE_UNIVERSE_RESOLVER | engine builds per-strategy universes via resolver; clamp superseded |
| OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE | collector prices fetch = no-floor envelope; fundamentals/insider = adopted-union |
| OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE | sentiment universe widened by adopted-union (live+candidate) |
| OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE | options archive = options-eligible ∩ live union |

## 1. MERGE + MIGRATE
- merge feat/sp7-phase-c-live-wiring → live branch; push
- `npm run db:migrate` then VERIFY (runner wart): `python3 -c "import os,psycopg2;from dotenv import load_dotenv;load_dotenv('.env');c=psycopg2.connect(os.environ['POSTGRES_URI']);cur=c.cursor();cur.execute(\"SELECT to_regclass('universe_shadow_parity')\");print(cur.fetchone())"`
- ⚠️ merge-conflict watch: run_sentiment_step.py has live uncommitted hunks (parquet datetime fix) — keep BOTH

## 2. SHADOW ON (same day as merge is fine — zero behavior change)
- .env: add `OPENCLAW_LIVE_UNIVERSE_SHADOW=1`
- `systemctl --user restart johnbot` (USER service — a stale SYSTEM unit exists, do not start it)
- next 10:00 ET cycle: confirm `universe_shadow_parity` rows ≈ strategy count
  `SELECT run_date, count(*), count(*) FILTER (WHERE NOT is_adopted AND jsonb_array_length(added_tickers)+jsonb_array_length(removed_tickers)>0) AS unadopted_drift FROM universe_shadow_parity GROUP BY 1 ORDER BY 1 DESC;`
- **MEMORY CHECK on the first shadow-ON cycle:** the sidecar builds a CoverageIndex (~1.65 GB transient, ~5 s) inside the signals step BEFORE load_prices. OOM on this 8 GB no-swap box is a SIGKILL (exit 137) — not catchable by the sidecar's try/except. Watch the cycle log for a clean signals step; if it 137s, set OPENCLAW_LIVE_UNIVERSE_SHADOW=0 and investigate co-resident memory.

## 3. SHADOW WATCH (≥3 trading days)
- daily: `python3 -m src.system_checks --check universe_shadow_parity`
- target: PASS "3 day(s) clean". WARN = drift (diagnose per-ticker via added/removed columns; remedies: widen default predicate / adopt the strategy / fix universe_config category). FAIL = resolve_error → code bug, fix before proceeding.
- DECISION RULE for the one known systematic diff (clamp keeps in_sp500 names with <60 bars; resolver's floor excludes them — verified EMPTY at plan time 2026-06-07, but a reconstitution adding a recent IPO can create it mid-shadow): a WARN is ACCEPTABLE FOR FLIP iff every removed ticker across all un-adopted rows is sub-floor. Classify with:
  `python3 -c "import pandas as pd; c=pd.read_parquet('data/master/prices.parquet',columns=['ticker']).groupby('ticker').size(); print(sorted(c[c<60].index))"`
  vs `SELECT DISTINCT jsonb_array_elements_text(removed_tickers) FROM universe_shadow_parity WHERE NOT is_adopted AND run_date >= current_date - 3;`
  Rationale: <60-bar names cannot fill strategy lookback windows, so zero-SIGNAL-delta (the spec's criterion) still holds. Record the classification in the flip-prereq notes. Any removed name NOT sub-floor = real drift, no flip.
- adoptions landing mid-window (ladder auto-adopt) do NOT reset the clock — adopted rows never gate.

## 4. C2 FLIP (collector envelope) — AFTER ladder drained + adoptions decided, ≥1 trading day BEFORE C1
- prereq: `redis-cli get sp7:ladder:last_full_run` non-nil; adoptions decided (universe-recs all resolved)
- .env: `OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE=1`; restart johnbot
- NOTE this gate also moves fundamentals (FMP) + insider (EDGAR) scope to the floored adopted-union at the same moment (spec §5) — confirm `adopted-union scope: fundamentals=N insider=M` in the log
- next collect: grep log `envelope: resolver=N config=M excluded=K final=F`; watch collect wall time (first post-tier_liquid-adoption run is the soak point)
- `python3 src/maintenance/doctor.py | grep collector_envelope`
- **EXPECTATION: delta=0 today** — universe_config active equity (~5,082) is a STRICT SUPERSET of the resolver envelope (~503); the merged list equals config until tier adoptions widen the resolver side. The flip is correct but inert; confirm the `envelope: resolver=N config=M ...` log line appears, do NOT expect the fetch list to change.
- **FMP quota note:** adopted names with no FMP fundamentals re-fetch daily (no coverage row is written on empty returns — pre-existing behavior, newly reachable); bounded by the 250/day quota. Watch for sustained quota WARNs after broad-tier adoptions.

## 5. C1 FLIP (engine per-strategy universes)
- PREREQS (all): ladder drained; adoptions decided; `universe_shadow_parity` = PASS 3 days; C2 ON ≥1 trading day; `universe_tier_coherence` PASS
- .env: `OPENCLAW_LIVE_UNIVERSE_RESOLVER=1`; restart johnbot
- observe 1 cycle: log `live-universe ON: union N tickers, 67 strategies, 0 fail-open`; signal counts sane vs prior day; NO empty-universe warnings
- rollback: gate =0 + restart (shadow resumes)
- **Capture the signals-step subprocess PEAK RSS on the observe cycle** (e.g. `grep -i 'maximum resident'` or `/usr/bin/time -v` wrapper, or systemd-cgtop snapshot) — confirm 8 GB headroom. NOTE: the clamp (OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500, still ON during the observe window) CAPS adopted-tier widening: an adopted tier_r3000 strategy stays clamped to ≈sp500 until §6 deletes the clamp. The observe cycle therefore validates PARITY and no-empty-universes ONLY — adopted breadth appears after §6.

## 6. CLAMP DELETION (immediately after a clean flipped cycle — DELETE, not gate-off)
- `git rm src/execution/universe_clamp.py tests/execution/test_universe_clamp.py`
- adapt `tests/execution/test_live_universe.py::test_unadopted_equals_clamp_output` — it imports `clamp_universe` for the differential check and would orphan post-deletion; by deletion time it has served its purpose: replace the clamp call with the expected literal set (or delete the test, keeping the other 6)
- engine.py: remove the two clamp lines (`from execution.universe_clamp import clamp_universe` / `universe = clamp_universe(universe)`) and the now-dead OPENCLAW_LIVE_UNIVERSE_SHADOW sidecar block (shadow has nothing to diff post-clamp)
- .env: remove OPENCLAW_ENGINE_UNIVERSE_CLAMP + OPENCLAW_LIVE_UNIVERSE_SHADOW
- grep-verify: `grep -rn "universe_clamp\|ENGINE_UNIVERSE_CLAMP" src/ tests/ scripts/` → only historical docs
- commit + restart; universe_shadow_parity TABLE stays (audit history — never delete)
- **NOTE:** Post-deletion + broad-tier adoption, each adopted strategy's panel slice is a per-iteration pandas copy (~100 MB at a ~5k-col union; one alive at a time). Watch the first wide cycle's RSS.

## 7. C3 FLIPS (individually, any time after C2; each independently revertible)
- `OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE=1` → next sentiment cycle: log `sentiment: resolver universe +N`
- `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE`: **DO NOT FLIP YET** — options_eligible is FALSE for all metadata rows (the chain-probe producer was never built; Phase D backlog). The gate-ON path logs 'gate ON but 0 options-eligible' and falls back to universe_config — flipping it today is a no-op plus one warning per run. Flip only after the Phase D eligibility producer ships.
- fundamentals/insider scoping rides the C2 gate (already ON)

## 8. ROLLBACK MATRIX
| Symptom | Action |
|---|---|
| shadow rows missing / errors | it's non-fatal — check logs; OPENCLAW_LIVE_UNIVERSE_SHADOW=0 silences |
| collect step slow / quota burn | OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE=0 + restart |
| signals anomalies post-C1-flip | OPENCLAW_LIVE_UNIVERSE_RESOLVER=0 + restart (pre-deletion); post-deletion: git revert the deletion commit |
| sentiment/archive issues | respective gate =0 |
