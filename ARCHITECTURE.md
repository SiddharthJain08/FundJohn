# FundJohn — System Architecture

> **System**: FundJohn / OpenClaw v2.0 · **Last verified**: 2026-07-17
> Companion docs: [README.md](README.md) · [docs/bootstrap.md](docs/bootstrap.md) ·
> [AGENTS.md](AGENTS.md) · historical: [LEARNINGS.md](LEARNINGS.md),
> [docs/archive/changelog.md](docs/archive/changelog.md)

## 1. Topology

```
                        ┌──────────────────────────┐
                        │         OPERATOR         │
                        │  Discord · :80 dashboard │
                        │  :7870 control room      │
                        └───────────┬──────────────┘
                                    │
   ┌────────────────────────────────┼─────────────────────────────────┐
   │  johnbot.service (ROOT USER SCOPE — the only always-on brain)    │
   │  src/channels/discord/bot.js                                     │
   │   ├─ Discord command routing (flash.js quick lane / main.js PTC) │
   │   ├─ hosts the :3000 API+dashboard (src/channels/api/server.js,  │
   │   │   nginx :80 → :3000, SSE)                                    │
   │   ├─ node-cron scheduler (src/engine/cron-schedule.js)           │
   │   └─ LangGraph graphs (src/agent/graphs/, PostgresSaver ckpts)   │
   └──────────────────────────────────────────────────────────────────┘
        │                    │                        │
        ▼                    ▼                        ▼
  fundjohn-dashboard   mastermind-chat         finbert-sentiment
  :7870 (root)         :7871 (claudebot)       :7872 (claudebot)
        │
        ▼
  ~40 openclaw-* system-scope services/timers (docs/systemd/ snapshot)
        │
        ▼
  Docker: openclaw-postgres (pgvector/pgvector:pg16) + openclaw-redis (redis:7)
  Broker: Alpaca paper API via the alpaca CLI (/root/go/bin/alpaca)
```

Everything runs from the working tree at `/root/openclaw` on branch `main`
(every unit sets `WorkingDirectory=/root/openclaw`). Long-running services
pick up code on restart; timer-spawned scripts pick up the tree on next fire.

## 2. Agents

Model wiring: `src/agent/config/models.js` + `src/agent/config/subagent-types.json`.

| Agent | Model | Surface |
|---|---|---|
| BotJohn | claude-sonnet-4-6 | Orchestrator/PM — Discord bot, maintenance runs (`src/agent/run_maintenance.js`) |
| TradeJohn | claude-sonnet-4-6 | Per-ticker confirmer inside `regime_blended_sizer_live.py` — approve/veto/scale, LOW_VOL/TRANSITIONING only, fail-open, budget-capped |
| PaperHunter | claude-sonnet-4-6 | Per-paper extraction with 4 rejection gates (`src/agent/graphs/paperhunter.js` fan-out) |
| StrategyCoder | claude-sonnet-4-6 | Strategy implementation + registration |
| MastermindJohn | claude-opus-4-7 (1M) | Weekend research modes (`src/agent/curators/run_mastermind.js`), code review, interactive chat service |

ResearchJohn was retired 2026-05-02 (`corpus-curator` remains an alias for
mastermind). LLM spend is dollar-budgeted (`src/budget/`, Redis `budget:mode`
GREEN/YELLOW/RED); standing orders live in [AGENTS.md](AGENTS.md).

## 3. Deterministic pipeline & regimes

**Daily cycle** — 10:00 ET Mon–Fri, LangGraph-dispatched over
`src/execution/pipeline_orchestrator.py` (accepts `--steps <subset> --reason
<tag>` for partial runs):
`collect → [sentiment] → signals → ic_gate → handoff → trade → alpaca →
reconcile → report → pyportfolioopt_shadow → health`.

**Regime detection** (regime change ⇒ *redeploy*, never liquidation):
- Daily HMM (`scripts/run_market_state.py`, 9:00 ET) — read-only regime write.
- Intraday HMM (`scripts/run_intraday_market_state.py`, every 5–15 min
  9:00–19:00 ET) — **regime of record** since 2026-06-08; on a confirmed
  transition (hysteresis + confidence + cooldown) it spawns
  `scripts/redeploy_pipeline.py` (`signals→handoff→trade→alpaca→reconcile`).
  The sizer's `delta = target − current_broker_position` nets positions.
- Crypto HMM (hourly, 24/7) with its own redeploy lane; equity book is
  structurally invisible to it.
- Forced liquidation is operator-only (`scripts/run_forced_liquidation.sh`).

**Other lanes** (see `src/engine/cron-schedule.js` + `docs/systemd/`):
15:55 ET correlation de-gross (threshold 0.6 / cap 0.20), 16:15 ET EOD
compute, EOD collector with a freshness gate scoped to the
**strategy-consumed universe** (not the wide resolver envelope — the 2026-07
starvation fix), pre/post-market take-profit passes, EDGAR 8-K scans,
premarket scans, nightly amcheck, split watcher, SP-7 overnight ladder.

## 4. Sizing & risk

`src/execution/regime_blended_sizer.py` (+ thin `_live` wrapper) is the
production `trade` step:

1. **Weights** — per-regime `effective_sharpe` (backtest-only since
   2026-07-16) × trade-count factor √(ln n/1000), clamped by a plausibility
   cap from `pipeline_config`.
2. **Conviction gate** — `S_adj = Σwᵢ²dᵢ / √(Σwᵢwⱼdᵢdⱼρᵢⱼ)` per ticker vs the
   per-regime floor `regime_sizer_params.min_corr_cum_sharpe` (sole gate since
   2026-07-01; dashboard-controlled).
3. **Brackets** — effective-Sharpe-weighted stacked stops/targets
   (`src/execution/bracket_stacking.py`); session-aware executor
   (`alpaca_executor.py`) with DTBP guard (min of buying_power, RegT) and
   extended-hours limit routing; broker-side OCO stops (read them with
   `alpaca order list --nested`).
4. **Instrument classes** — equity/etp/option/crypto routing
   (`instrument_class_sizer.py`); crypto has its own execution lane and
   fractional-qty handling.

## 5. Data layer

- **Master parquets** (`data/master/*.parquet` — prices, options_eod,
  financials, macro, insider, earnings, prices_30m, historical_regimes,
  crypto_bars_1h): **append-only, never delete** (see CLAUDE.md invariant).
  Reads are DuckDB-bounded; never load whole files on the 8GB box.
- **Postgres** (docker, pgvector image): 142 numbered idempotent migrations in
  `src/database/migrations/` applied three ways (compose initdb mount, boot
  `migrate()`, `npm run db:migrate`). Canonical append-only tables:
  `execution_signals`, `signal_pnl`, `alpaca_submissions`, `data_coverage`,
  `strategy_registry`, `research_corpus`. `strategy_backtest_trades` is bulky
  but regenerable by re-backtest.
- **Redis**: budget mode, locks, cooldowns, handoff cache, checkpoints — all
  regenerable.
- **Universe** — the SP-7 `UniverseResolver` is the sole live-universe
  authority: per-strategy predicates `universe_filter(meta, as_of)` over
  `ticker_metadata_snapshots`; a wide fetch envelope keeps history accruing
  while the freshness gate is scoped to what strategies actually consume.
- **Coverage truth** — `data_coverage` advances only after parquet writes
  durably commit (crash ⇒ re-fetch, never a silent hole).

## 6. Research pipeline

Discovery (`arxiv_discovery.py`, `openalex_discovery.py`, paper-expansion
sweeps) → `research_corpus` → Mastermind corpus rating → PaperHunter
extraction gates → StrategyCoder implementation → backtest
(`unified_backtest.py`: true-MTM daily marks, class-aware costs, optional
resolver-scoped universe) → **per-regime sleeve promotion gate** (positive
regime Sharpe, ≥trade floor, class max-DD) → registry approval. Weekend
automation: Saturday backtest-coupled auto-adjustment (apply only on strictly
positive ΔSharpe; confidence >0.8 auto-approval, else `noted`), Sunday
auto-approval lane, Monday 00:00 ET activation assigner. The four historical
per-mode Saturday timers are installed but disabled — superseded by the
sunday-research split (currently swapped onto Saturday via
`docs/systemd/weekend-swap/`).

## 7. Deployment & operations

- **Replication**: `docs/bootstrap.md` is the runbook; `scripts/install_systemd.sh`
  installs the unit fleet; `docs/nginx/openclaw.conf` fronts the dashboard.
  Clone path `/root/openclaw` and the alpaca CLI at `/root/go/bin/alpaca` are
  hard requirements (absolute-path lattice).
- **johnbot runs in ROOT USER scope** (`XDG_RUNTIME_DIR=/run/user/0
  systemctl --user …`). Never enable the system-scope johnbot copy —
  split-brain double bot.
- **Integrity**: `src/security/integrity.js` hash-verifies CLAUDE.md,
  AGENTS.md, IDENTITY.md, SOUL.md at boot; regenerate the machine-local
  manifest after editing them (`npm run integrity:generate`).
- **Diagnostics**: `src/maintenance/doctor.py` (fast preflight),
  `python3 -m system_checks` (live probes by tag), daily health digest, and
  `openclaw-failure-notify@` OnFailure hooks on the important units.
- **Resource envelope**: 2-core / 8GB / no-swap VPS — serialize heavy work
  (`nice -n 19`), several maintenance units need ~4GB headroom and must not
  run concurrently with fleet re-backtests.
