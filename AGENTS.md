# OpenClaw — Agent Standing Orders

Deterministic behavioral rules embedded in all agent contexts.
No API calls, no token spend. Enforced at the start of every operation.
Last verified against HEAD: 2026-07-17.

---

## STANDING ORDERS

### SO-1: Budget Mode Gate
Check Redis key `budget:mode` before any non-essential LLM operation.
- **GREEN**: proceed normally.
- **YELLOW**: reject non-essential operations. Respond: "Budget YELLOW ($X/day) — only essential ops permitted."
- **RED**: reject all operations except operator-manual triggers. Respond: "Budget RED — operator must approve."

### SO-2: Execution Gate (registry, not manifest)
The engine trades strategies whose **DB row** `strategy_registry.status = 'approved'` —
NOT the manifest `state`. `src/strategies/manifest.json` state (`live`, `candidate`,
`deprecated`, …) is lifecycle metadata; treating it as the execution gate is a
documented recurring mistake. Promotion to `approved` flows only through the
per-regime sleeve gate (positive Sharpe in the activated regime, minimum trade
count, per-class max-drawdown limits) — see `src/lib/promotion_service.js` and
`src/strategies/lifecycle.py`. `deprecated`/`archived` strategies are never
deployed; deprecation is a flag, never a file deletion (append-only invariant).

### SO-3: Conviction Gate
The regime-blended sizer sizes only tickers whose corr-adjusted cumulative
Sharpe (`S_adj`) clears the per-regime floor `regime_sizer_params.min_corr_cum_sharpe`.
This is the SOLE conviction gate (legacy cumulative-Sharpe machinery removed
2026-07-01). Floors are dashboard-controlled; do not hardcode them.

### SO-4: TradeJohn Confirmer Contract
The per-ticker LLM confirmer inside `regime_blended_sizer_live.py` may
approve / veto / scale individual orders. It runs only in LOW_VOL and
TRANSITIONING regimes, is budget-capped per invocation, and is **fail-open**:
an LLM error must never block the deterministic pipeline.

### SO-5: Max-Drawdown Escalation
Per-instrument-class drawdown limits gate promotion (see
`PROMOTION_THRESHOLDS` in `src/strategies/lifecycle.py`). A live strategy
breaching its class limit is flagged in the Saturday comprehensive review and
escalated to the operator in Discord; auto-adjustments apply only through the
backtest-coupled Saturday lane (strictly-positive ΔSharpe required).

### SO-6: Fail-Open Must Be Loud
Broad `except Exception` fail-open handling is allowed on the trading path
ONLY with a machine-greppable log tag. Silent success (rc=0 with nothing done)
has caused multi-day incidents; when a step can no-op, it must say so in the
journal and, where wired, the failure-notify hook.

### SO-7: Token Economy (Cache-First)
Only pay for novel work. Check the Redis handoff layer (`handoff:{date}:{stage}`)
before re-running upstream computation. `cache_read_input_tokens` is tracked in
`subagent_costs`; PaperHunter is hard-capped per invocation.

### SO-8: Secrets
`.env` is root-only and never committed. Webhook URLs are credentials. The
secret-redaction middleware strips known secrets from agent contexts; never
paste raw `.env` contents into prompts, Discord, or documents.

---

## AGENT CHAIN OF COMMAND

```
Operator (Discord / dashboards)
└── BotJohn (claude-sonnet-4-6) — orchestrator + PM; src/channels/discord/bot.js
    ├── Deterministic daily pipeline (10:00 ET, LangGraph-dispatched):
    │     collect → [sentiment] → signals → ic_gate → handoff
    │     → trade (regime_blended_sizer_live) → alpaca → reconcile
    │     → report → pyportfolioopt_shadow → health
    │     └── TradeJohn confirmer (claude-sonnet-4-6) — SO-4, inside `trade`
    ├── Weekend research (sunday-split lanes, systemd timers):
    │     ingest (saturday_brain phases 0–4) → code (finisher phases 5–8)
    │     ├── MastermindJohn (claude-opus-4-7, 1M ctx) — corpus rating,
    │     │   comprehensive review, position recs, paper expansion, code review
    │     ├── PaperHunter (claude-sonnet-4-6) — per-paper extraction, 4 gates
    │     └── StrategyCoder (claude-sonnet-4-6) — implementation + registration
    ├── Maintenance (run_maintenance.js --mode daily|saturday|saturday-verify|weekend-*)
    └── MasterMind chat service (:7871) — interactive research, dashboard Research tab
```

Model assignments live in `src/agent/config/models.js` (+
`src/agent/config/subagent-types.json`); the table above is descriptive, the
code is authoritative. ResearchJohn was retired 2026-05-02; `corpus-curator`
is a legacy alias for `mastermind`.

BotJohn has final authority over strategy lifecycle transitions, subject to
the operator's Discord/dashboard controls. Paper trading is fully automatic —
there is no per-trade human approval gate.
