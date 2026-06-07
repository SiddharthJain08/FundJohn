# SP-7 Phase B activation runbook (operator-gated)

Everything in the `feat/sp7-phase-b-tier-ladder` branch is inert until these
steps run. Order matters. Built 2026-06-07; spec
`docs/superpowers/specs/2026-06-06-sp7-phase-b-tier-ladder-design.md`.

Pre-flight: weekend stack NOT running (`systemctl list-timers`),
`data/.sp7_backfill_armed` absent, branch merged into the live branch.

## 1. MERGE + MIGRATE

- merge `feat/sp7-phase-b-tier-ladder` into the live branch; `git push`
- `npm run db:migrate`
- VERIFY (the runner has no applied-tracking — trust nothing):
  ```
  python3 -c "import psycopg2,os;c=psycopg2.connect(os.environ['POSTGRES_URI']).cursor();c.execute('SELECT 1 FROM universe_ladder_runs LIMIT 0');c.execute('SELECT 1 FROM universe_threshold_proposals LIMIT 0');print('ok')"
  ```

## 2. B0 REPAIR (off-window or nights; ~1–3 h total)

a. Dry-run one month, sanity the counts:
   ```
   OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py \
       --months --start 2023-06-01 --end 2023-06-30 --dry-run
   ```
   Expected (verified at build time): `rows=4397 changed=2288 inserted_missing=116 in_sp500_total=476`.

b. **ONE month LIVE first** (review gate — the write SQL's first live execution):
   run the same month WITHOUT `--dry-run`, then re-run WITH `--dry-run` —
   changed must drop to ~0 and the committed counts must match step a's numbers.

c. Full repair: `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 nice -n 19 python3 scripts/sp7_b0_repair_metadata.py --months --dailies --resume`

d. ACCEPTANCE (spec §3, criterion 2 as re-bounded 2026-06-07):
   - `python3 -m system_checks --check universe_tier_coherence` → **PASS**
     (mega-caps in_r1000 at 2021-07-31 / 2023-06-30 / 2025-06-30 + no degenerate dailies)
   - in_sp500 per month ≥ 460 AND ≥95% of CSV-reconstructable members
     (structural max ≈476 — Wikipedia-CSV ceiling; BACKLOG: fuller SP500 history)
   - r1000 = 1000; r3000 = min(3000, ranked-pool) — any month r3000 < 2800 →
     investigate BEFORE ladder GO
   - clamp live-parity: next engine compute logs kept≈591 (B0 must not move it —
     the clamp reads the latest snapshot, untouched except 05-25..06-04 dailies)

## 3. DASHBOARD RESTARTS (both adoption paths currently 404)

- `systemctl restart fundjohn-dashboard.service` (or `--user` — match the unit)
  → VERIFY `curl -s localhost:7870/api/universe-recs | head -c 200` is JSON, not 404
  → VERIFY `curl -s localhost:7870/api/universe-ladder` → `{"run_id":null,...not migrated...}` pre-migration or real JSON post-migration
- restart johnbot (loads the :3000 B3 endpoints):
  `systemctl --user restart johnbot.service`
  → VERIFY `curl -s localhost:3000/api/universe-threshold-proposals` → JSON

## 4. DISCORD WEBHOOK WIRING (build-time finding: key is MISSING live)

`agent_registry.webhook_urls` has NO `universe-recs` key on the live DB
(checked 2026-06-07) — without it, ladder rec posts silently skip (non-fatal).
Either:
- create a Discord webhook for #universe-recs and add it to botjohn's
  `webhook_urls` jsonb under key `universe-recs`, OR
- set `DISCORD_UNIVERSE_RECS_WEBHOOK=<url>` in `.env` (env override path).
VERIFY: `python3 -c "import sys; sys.path.insert(0,'.'); sys.path.insert(0,'src'); import psycopg2,os; from backtest.universe_ladder_recs import get_webhook; print('webhook found:', bool(get_webhook(psycopg2.connect(os.environ['POSTGRES_URI']))))"` → True

## 5. LEGACY CLEANUP + B3 INTEGRATION GATE

- `python3 scripts/supersede_legacy_universe_recs.py` → "tagged 58"
- **B3 integration gate (mandatory — first live execution of the detached child's
  DB path):** `time python3 -m src.execution.universe_threshold_proposals manual-check`
  → expect `factor=1.0`, `proposals=0` (no-op pre-adoption), no traceback; wall
  ~30–60 s. (This is why the adoption hook is a DETACHED spawn — it exceeds the
  30 s execFileSync window.)

## 6. LADDER UNITS

- `cp docs/sp7-ladder.service docs/sp7-ladder.timer ~/.config/systemd/user/`
- `systemctl --user daemon-reload && systemctl --user enable --now sp7-ladder.timer`
- VERIFY `systemctl --user list-timers | grep sp7-ladder` → next Mon-Fri 01:00 UTC

## 7. SINGLE-STRATEGY SMOKE (before trusting the unattended loop)

ONLY after step 2d acceptance passes. Pick a fast fixed-ticker strategy
(e.g. S_fomc_presell_spy_long):
```
python3 scripts/run_universe_ladder.py seed --strategy <sid> --arm
nice -n 19 python3 scripts/run_universe_ladder.py drain
```
(foreground; minutes — extremes run, middles skip degenerate)
VERIFY before proceeding: rec row exists with candidate_set_id `sp7b-1-<run_id>`
and verdict universe-independent/no_change; `[ladder] DONE` printed; no stuck
running cells; Discord summary posted (or skipped-with-log if step 4 deferred).
Single-strategy seeds never set `sp7:ladder:full_run_id`, so this does NOT mark
the 12-week cadence satisfied.

## 8. FULL SEED + ARM

```
python3 scripts/run_universe_ladder.py seed --arm
```
- builds the membership artifact (~minutes; starts one month before the window
  so day-one bars resolve), seeds 67×4 cells, arms `data/.sp7_ladder_armed`,
  sets `sp7:ladder:full_run_id`
- sanity the artifact JSON sidecar n_series: sp500 ≈ 460–510 per month
  (post-B0!), tier_liquid ≈ 3300–5200
- first cells run tonight 01:00 UTC; watch `logs/sp7_ladder_<date>.log`

## 9. NIGHTLY WATCH (est. 3–10 nights; queue is resumable, estimate not load-bearing)

- recs post to #universe-recs as strategies finalize (changes only; no-change
  verdicts batch into one summary at drain end)
- adopt via ✅ reaction or :7870 buttons; each adoption fires a DETACHED B3
  refresh → proposals appear next to the Conviction Gates sliders on :3000
  (Apply/✗ buttons; [1,10] clamp enforced server-side + DB CHECK)
- `[sp7-ladder] COMPLETE — disarmed` in the log = full run done; redis
  `sp7:ladder:last_full_run` set → the 12th-Saturday cadence starts (the
  weekend step-8 sentinel ALSO self-gates on the coherence probe, so it can
  never auto-seed on broken data)

## ABORT

Remove `data/.sp7_ladder_armed` (stops the next window; the running cell is
TERM-reaped at 13:00 — the driver's SIGTERM handler kills the child; the cell
resets to queued on next drain). B0 is idempotent + resumable; its writes are
UPDATEs/INSERTs only — never deletes.

## Build-time evidence (for the reviewer/operator)

- B0 one-month dry-run verified live: `2023-06-30 rows=4397 changed=2288 inserted_missing=116 in_sp500_total=476`
- coherence probe verified FAILING pre-B0 (all 4 mega-caps, all 3 probe months) — proves detection
- tier-mode grid cell verified end-to-end live (momentum_12_1, sp500, real metrics + trade_sha)
- B3 union-resolve timed live: 31.3 s (validates the detached-spawn design)
- `timeout --signal=TERM` → rc=124 verified empirically even with the driver's 143-exit handler
