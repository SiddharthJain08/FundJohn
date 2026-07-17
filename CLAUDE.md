# CLAUDE.md — FundJohn / OpenClaw System

This project contains all system optimizations for FundJohn, a bot-network quantitative hedge fund built on OpenClaw using Claude Code agents.

## Core invariant — NEVER DELETE FROM THE MASTER DATABASE
The master parquets and Postgres tables under `data/master/` are append-only.
Columns may be ADDED at any time. Tickers may be ADDED at any time. Date
ranges may only grow. **No code path is allowed to drop columns, drop
tickers, truncate the date axis, deprecate columns, or delete rows from
prices.parquet, options_eod.parquet, financials.parquet, macro.parquet,
insider.parquet, earnings.parquet, prices_30m.parquet,
historical_regimes.parquet, or crypto_bars_1h.parquet.** Same rule applies to the canonical
Postgres tables (`execution_signals`, `signal_pnl`, `alpaca_submissions`,
`data_coverage`, `data_columns`). The system's job is to grow the data
forever and let strategies opt into whichever subset they need; it is
NOT to optimize storage by pruning. Any future "deprecation" must be a
flag (`active=false`) on a metadata row, never a `DELETE`.

## Recent Changes
The dated engineering changelog lives in `docs/archive/changelog.md` (moved
2026-07-17 — it had grown to ~60KB and belongs with the historical record).
Add new entries THERE, newest first. Load-bearing standing rules stay in this
file; anything time-stamped goes in the changelog.

## System Overview
Autonomous quant PM system + hardcoded data pipeline:
- **BotJohn** (Claude Code, this agent): Orchestrator and portfolio manager.
- **DataPipeline** (hardcoded, src/execution/pipeline_orchestrator.py): 10-step base daily cycle (10:00 ET) — `collect → signals → ic_gate → handoff → trade → alpaca → reconcile → report → pyportfolioopt_shadow → health`. `sentiment` is inserted between `collect` and `signals` when `OPENCLAW_SENTIMENT_INGEST=1` (ON in production → 11 steps). `ic_gate` early-exits unless `OPENCLAW_IC_GATE=1` (default-OFF). `trade` = `regime_blended_sizer_live.py` (LIVE production sizer; OPENCLAW_REGIME_BLENDED_LIVE=1 since 2026-05-12). `pyportfolioopt_shadow` = shadow alt-sizer (non-fatal, gated on OPENCLAW_PYPORTFOLIOOPT_SHADOW=1, never routes orders). `health` = `daily_health_digest.js`. The orchestrator also accepts `--steps <subset> --reason <tag>` so external triggers (intraday redeploy) can run a fragment of the cycle without re-running data collection. (queue_drain removed 2026-04-28; trade_parity_capture + correlation_sidecar removed 2026-05-20; legacy single TradeJohn LLM step replaced by formula sizer + per-ticker confirmer.)
- **Regime detection + redeploy** (since 2026-05-19): regime change no longer triggers liquidation — it triggers a delta-based pipeline redeploy via `scripts/redeploy_pipeline.py` (signals→handoff→trade→alpaca→reconcile). The sizer's existing `delta = target − current_broker_position` math means positions get netted, not blown out. Daily HMM (`scripts/run_market_state.py` at 9 AM ET) writes regime_latest.json + market_regime row only — read-only. Intraday HMM (`scripts/run_intraday_market_state.py` every 5 min, 9-19 ET) spawns the redeploy on confirmed transition with hysteresis(3)+confidence(<70%)+cooldown(60min) gates. After-hours redeploys (16:00-20:00 ET) gated by `OPENCLAW_REDEPLOY_EXTENDED_HOURS=1` (default OFF); when ON, alpaca_executor submits `--type limit --extended-hours` for eligible symbols and skips the rest. Total-portfolio liquidation is operator-only via `scripts/run_forced_liquidation.sh` → `regime_liquidator.py --force` (refuses outside RTH; poll-to-terminal audit).
- **TradeJohn confirmer** (claude-sonnet-4-6): Per-ticker approve/veto/scale confirmer inside `regime_blended_sizer_live.py`. Runs only in LOW_VOL/TRANSITIONING regimes. Fail-open on LLM error. Invoked via `claude-bin --print --output-format json --model sonnet --max-budget-usd 0.50`.
- **PaperHunter** (claude-sonnet-4-6, upgraded from Haiku 2026-04-23): Per-paper extraction + 4 rejection gates.
- **StrategyCoder** (claude-sonnet-4-6): On-demand strategy implementation.
- **MastermindJohn** (claude-opus-4-7, 1M ctx): Opus research orchestrator (`src/agent/curators/run_mastermind.js`) + the interactive chat service behind the dashboard Research tab (`mastermind-chat.service`, :7871).
  - Weekend research runs through the **sunday-research split** (currently swapped onto Saturday via `docs/systemd/weekend-swap/` timer overrides): `openclaw-sunday-research-ingest` (saturday-brain phases 0–4: expand→sweep→rate→recurate→hunt) then `openclaw-sunday-research-code` (finisher phases 5–8), plus `openclaw-sunday-code-review` (mastermind code review). The older per-mode timers (`mastermind-corpus`, `strategy-review`, `position-recs`, `paper-expansion`, `saturday-brain`) are installed but **DISABLED — superseded by the split**; the modes themselves (`corpus | comprehensive-review | position-recs | paper-expansion`) still exist on `run_mastermind.js` for manual runs.
  - Was `CorpusCurator` prior to 2026-04-22 Phase 3; legacy `corpus-curator` subagent type still resolves to the same prompt for backward compat.

## Context Retention
Retain all context and memory of:
- File locations on the VPS (/root/openclaw/)
- Current strategy lifecycle states (from manifest.json)
- Agent architecture and responsibilities
- Changes made and bottlenecks fixed

At every step maintain a complete understanding of:
- Which strategies are live/paper/deprecated
- What data collections are active
- Current portfolio state
- Any pending lifecycle transitions

## LangGraph Orchestration (added 2026-04-22)
The cycle and paper-hunt flows run through LangGraph.js:
- `src/agent/graph.js` — daily cycle StateGraph (datajohn → tradejohn → HITL → botjohn); PostgresSaver checkpointer in `langgraph` schema; `interruptBefore: ['botjohn']` for operator approval; conditional edge skips botjohn if tradejohn produced zero signals. (ResearchJohn retired 2026-05-02 — mastermind handles research via saturday_brain.js + comprehensive_review.js, not this graph.)
- `src/agent/graphs/paperhunter.js` — Send-based parallel fan-out for paper extraction.
- `src/agent/graphs/index.js` — graph registry. Add new flows here.
- `src/agent/traceBus.js` — in-memory event ring buffer fanning out to dashboard SSE.
- `bin/run-graph.js` — CLI runner: `node bin/run-graph.js list | cycle '<json>' | cycle:resume '<json>' | cycle:state <threadId>`.
- Dashboard: `src/channels/dashboard/server.js` on 127.0.0.1:7870 (systemd: `fundjohn-dashboard.service`). SSH-tunnel to view. Surfaces bots, subagents, analyses, verdicts, trades, checkpoints, workspaces, graph runs + live traces, HITL approve/veto buttons.
- Smoke tests: `node scripts/smoke/graph-smoke.js` (cycle HITL + veto), `node scripts/smoke/paperhunter-smoke.js` (fan-out parallelism).
- Set `LANGSMITH_API_KEY` in `.env` to auto-enable LangSmith tracing (project=`fundjohn`).

## Key Paths (VPS: /root/openclaw/)
- `src/strategies/lifecycle.py` — strategy state machine
- `src/strategies/manifest.json` — strategy registry
- `src/strategies/implementations/` — strategy Python files
- `src/agent/main.js` — agent orchestrator entry point
- `src/agent/prompts/subagents/` — agent prompt files
- `src/agent/curators/mastermind.js` — MastermindJohn corpus-mode orchestrator
- `src/agent/curators/comprehensive_review.js` — Saturday per-strategy memos
- `src/agent/curators/position_recommender.js` — Saturday sizing recs from memos
- `src/agent/curators/paper_expansion_ingestor.js` — Sunday Opus-steered paper hunt
- `src/agent/curators/run_mastermind.js` — CLI entry (`--mode {corpus|comprehensive-review|position-recs|paper-expansion}`)
- `src/agent/research/gate-decisions.js` — structured `paper_gate_decisions` emitter
- `src/ingestion/arxiv_discovery.py` — broad arXiv q-fin harvest into `research_corpus`
- `src/ingestion/openalex_discovery.py` — SSRN/NBER/JFE/RFS/JF/JFQA/QF + author watchlist + citation graph
- `src/database/migrations/032..038_*.sql` — corpus + calibration + ROI schema
- `docs/systemd/` — canonical snapshot of every installed systemd unit (system + user scope); see docs/systemd/README.md
- `src/system_checks/` — runnable diagnostic probes for live state. Tagged by domain (`pipeline`, `broker`, `regime`, `strategies`, `agents`, `storage`). `python3 -m system_checks [--tag X] [--check NAME] [--json]`. See `src/system_checks/README.md` for the contract. Distinct from `tests/` (pytest unit tests) and `src/maintenance/doctor.py` (sub-second preflight). When fixing a bug class that shouldn't recur, add a regression check here.
- `src/agent/run_maintenance.js` — BotJohn maintenance driver (doctor + system_checks + digest + investigate + fix + post to #general); dispatches on `--mode {daily,saturday,saturday-verify,weekend-sat,weekend-sun}` (daily = Mon-Fri 12:00 ET timer)
- `agents/` — agent identity and soul files
- `data/` — master parquet datasets
- `.env` — secrets and config

## Deployment Workflow
Production is the working tree at `/root/openclaw` on branch **main** (since
2026-07-17; every systemd unit uses `WorkingDirectory=/root/openclaw`).
Changes flow: edit on the VPS → commit → `git push origin main`. Long-running
services (johnbot and the :3000 dashboard it hosts, fundjohn-dashboard,
mastermind-chat, finbert) pick up code only on restart; timer-spawned scripts
pick up the working tree on their next run — so never leave the tree in a
half-edited state across a timer boundary.
