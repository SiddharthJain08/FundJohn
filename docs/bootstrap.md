# Bootstrap — replicate FundJohn/OpenClaw from a fresh clone

W8 deliverable (2026-07-02). Target: Ubuntu-class box, 2+ cores, 8GB+ RAM
(production runs on 2-core/8GB **no-swap** — see `reference_vps_two_core_cpu`
memory: serialize heavy work, `nice -n 19`, never load whole master parquets).

## 1. System dependencies

- **Python 3.13** + pip → `pip install -r requirements.txt`
  (curated runtime set; `requirements-vps-snapshot.txt` is the full
  production freeze if you need an exact replica. `scipy`/`redis` may come
  from OS packages on the production box.)
- **Node.js** (v20+) → `npm install` (package.json at repo root)
- **PostgreSQL** (prod runs 16.x) + **Redis** (localhost, no auth by default)
- **alpaca CLI** (Go): binary at `/root/go/bin/alpaca` (v0.0.9) — JSON output
  by default, NO `--json` flag. Env auth: `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`
  (pass to the CLI as `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`).
- **claude-bin** (`/usr/local/bin/claude-bin`) — Claude Code CLI; agents run
  as user `claudebot` (uid 1001). Needed for TradeJohn confirmer, research
  agents, maintenance runs.
- nginx (reverse-proxies :3000 → :80 for the user dashboard).

## 2. Configuration

    cp .env.example .env        # then fill every blanked (secret) key

`.env.example` is a sanitized copy of the full production config (81 keys):
flags/ports keep their production values; keys matching
KEY/TOKEN/SECRET/PASS/URI/OAUTH are blanked. `POSTGRES_URI` must point at
your DB; migrations in `src/database/migrations/` apply **idempotently at
boot** (there is NO migrations tracking table — verify by effect).

## 3. Services (systemd)

Canonical unit snapshot: **`docs/systemd/`** (see its README). Install:

    sudo cp docs/systemd/openclaw-* /etc/systemd/system/
    sudo cp -r docs/systemd/*.service.d /etc/systemd/system/
    mkdir -p ~/.config/systemd/user && cp -r docs/systemd/user/* ~/.config/systemd/user/
    sudo systemctl daemon-reload && systemctl --user daemon-reload

**johnbot (the Discord bridge + :3000 dashboard) runs in ROOT USER SCOPE**
(`systemctl --user enable --now johnbot`) — never enable the system-scope
copy (split-brain / EADDRINUSE). Other long-running user-scope services:
`fundjohn-dashboard` (:7870), `mastermind-chat` (:7871),
`finbert-sentiment` (:7872).

Most trading lanes are **in-process node-cron schedules inside johnbot**
(`src/engine/cron-schedule.js` + `bot.js`): 9:00 ET daily regime, 10:00 ET
daily cycle, 15:55 ET EOD sizing, 16:15 ET EOD compute, `*/5 9-16` intraday
HMM, hourly crypto, weekend research. The systemd timers cover the rest
(edgar, scans, refreshes, weekend maintenance) — enable the ones you want
after reviewing `systemctl list-timers`.

## 4. Data

`data/master/*.parquet` is **NOT in git** (append-only master data — see the
core invariant in CLAUDE.md: never delete/truncate). On a fresh box you must
either restore these from a backup of the production box or re-backfill:

- prices/metadata: SP-2 backfill machinery (`scripts/backfill_universe_5y.py`,
  `docs/sp2-backfill-runbook.md`)
- options EOD: self-archives daily going forward (16:30 ET timer); history
  before your bootstrap date is not recoverable from providers
- crypto bars: `src/ingestion/crypto_bars.py` backfill

Postgres content (registry, signals, trades, config) similarly needs a dump/
restore for a true replica; schema alone comes up empty-but-functional.

## 5. Verify

    python3 src/maintenance/doctor.py --quick        # sub-second preflight
    python3 -m system_checks --tag pipeline          # deeper probes
    curl -s localhost:3000/api/status                # dashboard up

Known-good state: johnbot user unit active (NRestarts=0), doctor freshness
checks green after the first collect cycle, `strategy_registry` populated.

## 6. Gotchas (hard-won)

- `.env` is NOT shell-safe — never `source` it (use `EnvironmentFile=` or
  per-key `grep`).
- Engine trades `strategy_registry.status='approved'` (DB), NOT manifest
  state (`feedback_manifest_vs_registry_execution_gate`).
- Master parquet writers must write atomically (tmp + `os.replace`) — the
  2026-06-29 corruption came from an in-place rewrite killed mid-write.
- Two-core box: run backtests/tests serialized, `nice -n 19`.
- Paper alpaca: OPG fills ~7% (auctions unsimulated — live differs);
  see `docs/w7-alpaca-live-readiness.md` before any live cutover.
