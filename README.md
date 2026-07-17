# FundJohn / OpenClaw

**An autonomous, self-hosted quantitative paper-trading system.** A
deterministic daily pipeline turns market data into sized, bracketed paper
orders at Alpaca with zero LLM tokens; a Claude-agent layer on top does the
judgement work — research synthesis, strategy authoring, weekend tuning, and
operator conversation over Discord. Everything runs on one VPS.

> Paper trading only. This repository is not investment advice, and nothing in
> it is warranted to make (or not lose) money. MIT-licensed — see
> [LICENSE](LICENSE).

*Docs verified against HEAD: 2026-07-17.*

## How it works

**Deterministic lane (no tokens).** The daily cycle runs at 10:00 ET
(Mon–Fri), dispatched through a LangGraph graph over
`src/execution/pipeline_orchestrator.py`:

```
collect → [sentiment] → signals → ic_gate → handoff
        → trade → alpaca → reconcile → report → pyportfolioopt_shadow → health
```

The `trade` step is the **regime-blended sizer**
(`src/execution/regime_blended_sizer_live.py`, production since 2026-05-12).
Around the daily cycle sit the other clock-driven lanes (all times ET; the
authoritative table is `src/engine/cron-schedule.js` plus the systemd timers in
`docs/systemd/`): a 9:00 daily-HMM regime write, an intraday HMM every 5–15
minutes (9:00–19:00) that triggers a delta-based **redeploy** (never a
liquidation) on a confirmed regime transition, a 15:55 correlation de-gross,
a 16:15 EOD compute, hourly crypto lanes, and pre/post-market take-profit
passes.

**Sizing & risk.** Positions are sized by per-regime strategy weights and
gated by `S_adj` — a correlation-adjusted cumulative Sharpe per ticker — with
per-regime floors that are dashboard-tunable. Weights carry a trade-count
factor (√(ln n/1000)); exits use effective-Sharpe-weighted stacked brackets;
a DTBP guard bounds buying power. Strategy backtest metrics come from
`src/backtest/unified_backtest.py` (true mark-to-market daily returns,
class-aware cost model).

**Agent lane (tokens, budget-capped).** Model assignments live in
`src/agent/config/models.js`:

| Agent | Model | Role |
|---|---|---|
| BotJohn | Sonnet | Orchestrator + PM; the Discord bot (`src/channels/discord/bot.js`) |
| TradeJohn | Sonnet | Per-ticker order confirmer inside the sizer (fail-open, LOW_VOL/TRANSITIONING only) |
| PaperHunter | Sonnet | Per-paper alpha extraction with rejection gates |
| StrategyCoder | Sonnet | Strategy implementation + registration |
| MastermindJohn | Opus (1M ctx) | Corpus rating, strategy review, sizing recs, paper expansion, interactive chat (:7871) |

**Strategy lifecycle.** Strategies are pure-Python classes under
`src/strategies/implementations/` (no network, no DB, never raise — see the
base-class contract in `src/strategies/base.py`). The registry of record is
`src/strategies/manifest.json` plus the Postgres `strategy_registry`; **the
engine trades rows with registry `status='approved'`**, promoted through
per-regime sleeve gates (positive regime Sharpe, minimum trade count,
per-class max-drawdown). Weekend automation tunes brackets on
strictly-positive backtest ΔSharpe (Saturday) and auto-approves qualifying
candidates (Sunday).

**Data.** Post-SP-1 provider matrix (2026-05-22): **Alpaca** (AAT Plus) is
primary for equity/crypto bars, options chain + EOD self-archive, news and
screener; **FMP** covers fundamentals, macro and reference data; **SEC
EDGAR** full-text filings; `yfinance` survives only inside
`src/ingestion/cboe_vol_indices.py` (CI-enforced). Master datasets are
append-only parquets under `data/master/` — the NEVER-DELETE invariant in
[CLAUDE.md](CLAUDE.md) is load-bearing. Postgres (pgvector, via
docker-compose) holds canonical tables built by 142 idempotent migrations;
Redis holds budget state, locks and checkpoints.

## Services & ports

| Port | Service | Unit |
|---|---|---|
| 80 → 3000 | User dashboard (nginx → Express, SSE) — hosted inside the johnbot process | `johnbot.service` (**user scope**) + `docs/nginx/openclaw.conf` |
| 7870 | Operator control-room dashboard | `fundjohn-dashboard.service` |
| 7871 | MasterMind interactive chat (dashboard Research tab) | `mastermind-chat.service` |
| 7872 | FinBERT-Tone sentiment scoring | `finbert-sentiment.service` |

Roughly 40 further `openclaw-*` system-scope services/timers drive the
scheduled lanes — `docs/systemd/` is the byte-exact snapshot of every
installed unit, and `scripts/install_systemd.sh` installs them all.

## Repository layout

```
src/
  strategies/      pure-Python strategy implementations + lifecycle state machine
  execution/       sizer, executor, brackets, reconcile, orchestrator
  backtest/        unified backtest engine, universe grid, options pricing
  pipeline/        collectors (EOD/intraday), backfillers, premarket scans
  ingestion/       provider clients (Alpaca, FMP, EDGAR, vol indices, discovery)
  database/        Postgres client + 142 numbered migrations (append-only)
  regime/          HMM regime detectors (daily, intraday, crypto)
  agent/           LangGraph graphs, curators, research orchestrator, maintenance
  channels/        Discord bot, :3000 API/dashboard, :7870 operator dashboard
  services/        FinBERT server, mastermind chat server
  system_checks/   runnable live-state diagnostics (python3 -m system_checks)
  maintenance/     doctor preflight, weekend shell lanes
docs/
  bootstrap.md     fresh-VPS replication runbook (start here)
  systemd/         canonical unit snapshot + installer notes
  nginx/           reverse-proxy config
  runbooks/        operational runbooks   ·  reference/  evergreen references
  archive/         historical specs/plans/reports + the engineering changelog
scripts/           operational scripts (timer-wired + manual tools; smoke/ = manual smokes)
tests/             pytest suite (3,204 tests) + node --test JS tests
```

## Running it

Full fresh-VPS instructions live in **[docs/bootstrap.md](docs/bootstrap.md)**
— host prerequisites, the docker-compose Postgres/Redis stack, `.env`
configuration, `scripts/install_systemd.sh`, the unit enablement set, and the
data restore/regeneration tiers. The short version:

```bash
git clone git@github.com:SiddharthJain08/FundJohn.git /root/openclaw
cd /root/openclaw
cp .env.example .env            # fill in keys
cp .mcp.json.example .mcp.json
docker compose up -d            # Postgres (pgvector) + Redis; migrations auto-apply
npm install && pip install -r requirements.txt
npm run integrity:generate
sudo bash scripts/install_systemd.sh
```

The clone path `/root/openclaw` is currently a hard requirement (absolute
paths in units and scripts).

## Testing

```bash
pytest -m 'not integration' -q   # unit tests (integration tests need live Postgres)
npm run test:js                  # node --test over tests/**/*.test.js
python3 -m system_checks         # live-state diagnostics (on a running system)
node scripts/smoke/graph-smoke.js  # manual smokes live in scripts/smoke/
```

## License

MIT © 2026 Siddharth Jain. See [LICENSE](LICENSE).
