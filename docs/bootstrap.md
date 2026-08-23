# Bootstrap — replicate FundJohn/OpenClaw from a fresh clone

Target: Ubuntu-class box, 2+ cores, 8GB+ RAM. Production runs on a 2-core/8GB
**no-swap** VPS — serialize heavy work (`nice -n 19`), never load whole master
parquets, and note several maintenance units peak near 4GB.

**Hard requirement:** clone to **`/root/openclaw`** and run as root. Absolute
paths are baked into every systemd unit (`WorkingDirectory=/root/openclaw`)
and ~130 source files (`/root/openclaw`, `/root/go/bin/alpaca`,
`/root/.claude/.credentials.json`). Relocating requires a sweep that has not
been done.

*Last verified: 2026-07-17.*

## 1. System dependencies

```bash
apt update && apt install -y git nginx docker.io docker-compose-v2 golang nodejs npm python3 python3-pip
```

- **Node.js v20+** (production: v22) → `npm install` at repo root.
- **Python 3.12+** (production: 3.13) → `pip install -r requirements.txt`.
  Heads-up: this pulls **torch CPU wheels (~2GB download)** for FinBERT.
  `docs/reference/requirements-vps-snapshot.txt` is the exact production
  freeze if you need a byte-replica.
- **Docker + compose — the DB stack is containerized.** Postgres runs as
  `openclaw-postgres` from the **pgvector/pgvector:pg16** image (extensions
  vector/btree_gist/amcheck come from the image; bare-metal Postgres will
  break migration 054 and the amcheck timers) and Redis as `openclaw-redis`
  (redis:7-alpine, appendonly). Both are defined in `docker-compose.yml`,
  which also mounts `src/database/migrations/` as `initdb.d` — **first
  `docker compose up -d` applies every migration automatically.**
  ⚠️ The compose default `POSTGRES_PASSWORD` is `password`; whatever you use,
  `POSTGRES_URI` in `.env` (and the inline URI in `mastermind-chat.service`)
  must match it, or everything fails auth quietly.
  There is deliberately no psql on the host — use
  `docker exec -it openclaw-postgres psql -U openclaw openclaw`.
- **alpaca CLI** (Go): `go install github.com/alpacahq/cli@v0.0.9` → binary
  must end up at `/root/go/bin/alpaca` (36+ call sites use that absolute
  path). JSON output by default, NO `--json` flag. Auth via
  `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` from `.env` (mapped to
  `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`).
- **Claude Code CLI** at `/usr/local/bin/claude-bin` (with `/usr/local/bin/claude`
  symlink; production pins 2.1.x). Run `claude login` as root once — the bot
  copies `/root/.claude/.credentials.json` to the `claudebot` user at runtime.
  Needed for the TradeJohn confirmer, research agents, and maintenance runs.
- **claudebot user**: `useradd -m -u 1001 claudebot`. Ownership choreography
  that matters: `data/` stays root-owned but `data/.cache` must be
  claudebot-writable (`mkdir -p data/.cache && chown claudebot:claudebot
  data/.cache`) — a missing chown here once silently no-opped a producer.
- **uvicorn** must resolve at `/usr/local/bin/uvicorn` (pip system install) —
  `finbert-sentiment.service` invokes it by absolute path. First FinBERT start
  downloads the `yiyanghkust/finbert-tone` model (~1GB) into
  `/home/claudebot/.cache/huggingface`; needs outbound HTTPS.
- **nginx**: install the dashboard proxy —
  `cp docs/nginx/openclaw.conf /etc/nginx/sites-available/openclaw && ln -sf
  ../sites-available/openclaw /etc/nginx/sites-enabled/ && rm -f
  /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx`.

## 2. Configuration

```bash
cp .env.example .env          # then fill every blanked (secret) key
cp .mcp.json.example .mcp.json
npm install
npm run integrity:generate    # machine-local integrity manifest (gitignored);
                              # without it boot logs a SECURITY warning
```

`.env.example` mirrors the production key set; keys matching
KEY/TOKEN/SECRET/PASS/URI/OAUTH are blanked. `POSTGRES_URI` must match the
compose credentials (see §1). Migrations also re-apply idempotently at johnbot
boot and via `npm run db:migrate` (no tracking table — verify by effect).
`.env` is NOT shell-safe — never `source` it.

## 3. Services (systemd)

Canonical unit snapshot: **`docs/systemd/`** (see its README). Install
everything with:

```bash
sudo bash scripts/install_systemd.sh
loginctl enable-linger root     # keeps user-scope units alive without a login
```

**johnbot (Discord bridge + the :3000 dashboard it hosts) runs in ROOT USER
SCOPE** — `XDG_RUNTIME_DIR=/run/user/0 systemctl --user enable --now johnbot`.
Never enable a system-scope johnbot copy (split-brain double bot).
`fundjohn-dashboard` (:7870), `mastermind-chat` (:7871 — set its inline
`POSTGRES_URI` when installing) and `finbert-sentiment` (:7872) are
**system-scope** services.

Most trading lanes are in-process node-cron schedules inside johnbot
(`src/engine/cron-schedule.js`): 9:00 ET daily regime, 10:00 ET daily cycle,
15:55 ET de-gross, 16:15 ET EOD compute, 5–15-min intraday HMM, hourly
crypto, nightly resets.

**Enablement set** (mirrors the production box; everything else stays
installed-but-disabled):

- *System services*: `finbert-sentiment`, `fundjohn-dashboard`,
  `mastermind-chat`.
- *System timers*: `openclaw-afterhours-tp-{premarket,postmarket,rth-reconcile}`,
  `openclaw-amcheck`, `openclaw-botjohn-maintenance`,
  `openclaw-edgar-8k-{0715,0845}`, `openclaw-edgar-shares`,
  `openclaw-fmp-profiles`, `openclaw-options-archive`,
  `openclaw-options-eligibility`, `openclaw-phase2d-nightly`,
  `openclaw-premarket-realized-backfill`, `openclaw-premarket-scan-{0730,0900}`,
  `openclaw-refresh-universe-sizes`, `openclaw-regime-live-pnl`,
  `openclaw-stop-reattach`, `openclaw-sunday-code-review`,
  `openclaw-sunday-research-{ingest,code}`, `openclaw-tradable-universe-refresh`,
  `openclaw-vol-indices`, `openclaw-weekend-maintenance-{sat,sun}`,
  `openclaw-weekly-strategy-weights`.
- *User scope*: `johnbot`, `openclaw-presence`, and the `sp7-ladder`,
  `sp7-overnight-backfill`, `sp7-split-watcher` timers.
- *Deliberately disabled* (superseded or operator-gated): the weekend
  actuator `openclaw-weekend-saturday`, `openclaw-weekend-sunday`,
  `openclaw-backtest-refresh`, `openclaw-eod-refresh`, the legacy per-mode
  Saturday research timers (`saturday-brain`, `mastermind-corpus`,
  `mastermind-critique`, `strategy-review`, `universe-recs`, `position-recs`,
  `paper-expansion`, `strategy-backtest-refresh`,
  `botjohn-saturday-maintenance`, `botjohn-saturday-verify`),
  `openclaw-sp5-cleanup`, `openclaw-gateway`.

⚠️ Timers with `Persistent=true` fire a catch-up run the moment they are
enabled — touch their stamp files first when a surprise run would be harmful
(see `docs/systemd/README.md`).

## 4. Data tiers

`data/master/*.parquet` and Postgres content are **NOT in git**. Three tiers:

**REQUIRED — restore or backfill:**
- `data/master/*.parquet` (~1GB; prices, options_eod, financials, macro,
  insider, earnings, prices_30m, historical_regimes, crypto_bars_1h).
  Restore from the source box (`rsync -a root@SOURCE:/root/openclaw/data/master/ data/master/`)
  or backfill prices fresh with `node scripts/backfill_price_history.js`
  (DuckDB-bounded; ~40min for 12k tickers × 5y on the production box).
  Options EOD history predating your bootstrap is provider-unrecoverable —
  the daily 16:30 ET self-archive only grows it going forward.
- Canonical Postgres tables (append-only): dump on the source box, restore on
  the new one —
  `docker exec openclaw-postgres pg_dump -U openclaw -t execution_signals -t signal_pnl -t alpaca_submissions -t data_coverage -t strategy_registry -t research_corpus openclaw > canonical.sql`
  then `docker exec -i openclaw-postgres psql -U openclaw openclaw < canonical.sql`.

**REGENERABLE — skip on a fresh box:** `strategy_backtest_trades` (~4GB, 90%
of the DB — rebuilt by re-backtest), all Redis keys, `.agents/` regime state
(next detector run), `data/.cache/options_eligibility.json` (Sat 06:00 UTC
timer), the `langgraph` checkpoint schema.

**SEED:** `npm run workspace:init` (research workspace),
`mkdir -p /root/.learnings`, and backfill `crypto_bars_1h` via
`src/ingestion/crypto_bars.py` **before** enabling `OPENCLAW_CRYPTO_REGIME=1`
(the first tick crashes without bars).

## 5. Verify

```bash
python3 src/maintenance/doctor.py --quick        # sub-second preflight
python3 -m system_checks --tag pipeline          # deeper probes
curl -s localhost:3000/api/status                # user dashboard (johnbot)
curl -s localhost:7870/ -o /dev/null -w '%{http_code}\n'   # operator dashboard
curl -s localhost:7871/health 2>/dev/null; curl -s localhost:7872/health 2>/dev/null
pytest -m 'not integration' -q                   # unit suite
```

Known-good state: johnbot user unit active (NRestarts=0), doctor freshness
checks green after the first collect cycle, `strategy_registry` populated.

## 6. Gotchas (hard-won)

- `.env` is NOT shell-safe — never `source` it (use `EnvironmentFile=` or
  per-key `grep`).
- Engine trades `strategy_registry.status='approved'` (DB), NOT manifest
  state.
- Master parquet writers must write atomically (tmp + `os.replace`) — a
  2026-06 corruption came from an in-place rewrite killed mid-write.
- Two-core box: run backtests/tests serialized, `nice -n 19`; the
  botjohn-maintenance / options-archive / refresh-universe-sizes units need
  ~4GB headroom and will OOM if run concurrently with a fleet re-backtest.
- Paper alpaca: OPG/MOO auction fills are unsimulated — paper fills prove
  little; see `docs/archive/reports/w7-alpaca-live-readiness.md` before any
  live cutover.
- `alpaca order list` HIDES OCO stop legs without `--nested`.
- Out of scope on the production box: `ollama` (installed but unused) and the
  `agentforge*` databases in the shared Postgres — a replica needs neither.
