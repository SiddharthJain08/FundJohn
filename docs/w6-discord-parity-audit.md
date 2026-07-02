# W6 — Discord-as-Viewer Parity Audit

**Date: 2026-07-02. Read-only audit. Goal: confirm Discord has zero functionality absent from the dashboards.**

**Bottom line: Discord is NOT a pure viewer.** The two headline flows are clean — `/approve-strategy` and dashboard `/transition` both route through `src/lib/promotion_service.js` (identical gate/force/registry-first/weights-rebuild), and universe-rec reactions are literally a client of `:7870 POST /api/universe-recs/:id/:action`; cycle HITL is dashboard-only. But ~20 inbound capabilities have no dashboard route, and **5 are execution-affecting writes**. Items 1–5 below should be ported (or the Discord commands consciously retired) before Discord can be declared a viewer + dashboard-mirroring input channel.

Sources: `src/channels/discord/bot.js`, `src/agent/flash.js`, `src/channels/discord/relay.js`, `src/channels/discord/ic_handler.js`; dashboards `src/channels/api/server.js` (:3000), `src/channels/dashboard/server.js` (:7870).

## Discord-only gaps (ranked; concrete route/UI to close)

1. **Registry pause/resume** — `/pause-strategy` + `/approve-strategy` resume-role flip `strategy_registry.status` (the actual engine trade-gate) with no dashboard path. Close: `:3000 POST /api/strategies/:id/pause|resume` calling `registry_sync.syncRegistryStatus` (the same helper Discord uses). Execution-affecting.
2. **Generic `pipeline_config` write** — flash `/config <key> <value>` + `/sigma-gate` can set any live sizing/gating key (e.g. `bt_sharpe_plausibility_cap`, `asset_corr_cap_enabled` kill-switch). Dashboard exposes only 3 keys. Close: `:3000 GET/PUT /api/config/:key` with allowlist + audit.
3. **`corr_cumsharpe.live` diagnostics** — per-regime `|S_adj|` dist/live_keep/rec_floor (`regime_blended_sizer.py:1199-1213`) exists only as a #botjohn-log line (self-expires via `corr_cumsharpe_log_until`); the owed floor re-tune depends on it. Close: persist the metrics dict to a table + `:3000` tile next to the corr-floor sliders. *(Partially mitigated 2026-07-02: step stderr now persists to `logs/daily_cycle_steps_*.log` on success, commit `19c33f1`.)*
4. **Data-column approval** (`/approve-data`, `/veto-data`) — only way to unblock DataWiringAgent on `data_ingestion_queue` PENDING rows. Close: `:3000 GET /api/data-queue` + `POST /api/data-queue/:id/approve|veto` (reuse `_wireColumn`).
5. **Collector control** (`/pipeline pause|resume`) — the `:3000` "⏸ Pause" button only pauses the SSE log pane. Close: `POST /api/pipeline/pause|resume` (persist `collection_enabled`).
6. **Saturday memos + sizing recs** — `strategy_memos` and `strategy_sizing_recommendations` readable only in Discord history. Close: read-only panels on Research/Strategies tabs.
7. **Data-task lane** (`/fetch`, `/fill`, `/data`, `/data-status`) — add `GET/POST /api/data-tasks` or accept.
8. **Research ops triggers** (`/curator run|promote|re-curate`, `/diligence`, `/trade`, `/strategy-report`, `/engine-run`) — timers cover scheduled paths; accept or add a `:7870` run-job panel.
9. **Read-only funnels/ROI/cost views** (`/hit-rate`, `/data-roi`, `/data-demand`, `/curator calib|sample`, `/spend`, `/cost`, `/budget`) — SELECT-only; close opportunistically with tiles.
10. **Notification mirrors** (#circuit-breaker fires, executor error alerts, EDGAR 8-K digests, amcheck/split alerts) — add a generic alert-event log route or consciously accept as notification semantics.
11. **Conscious accepts (Discord-native/ops):** steering injection, freeform BotJohn PM tasking + images, `/git sync`, `/shutdown`, `/refresh-map`, `/approve-dataset` (legacy), `/adjust-strategy` + `/strategy-versions` (legacy versioning lane).

## Outbound Discord-only surfaces

| Surface | Producer | Status |
|---|---|---|
| `corr_cumsharpe.live` per-regime dist (#botjohn-log) | `regime_blended_sizer.py` | Discord-only; floor re-tune depends on it (see gap 3) |
| `strategy_memos` Saturday memos (#strategy-memos) | `comprehensive_review.js` | table not on any dashboard |
| `strategy_sizing_recommendations` (#position-recommendations) | `position_recommender.js` | table not on any dashboard |
| Circuit-breaker fire events (#circuit-breaker) | `position_circuit_breaker.py` | config on dashboard, fire-log Discord-only |
| EDGAR 8-K digests (#pre-market-alerts) | `ingest_edgar_8k.py` | `/api/db/news` serves Alpaca news only |
| Re-backtest progress, data-task progress, amcheck/split alerts | various | Discord-only |

PARITY (fine): #pipeline-feed ↔ Pipelines tab; #universe-recs ↔ `:7870` universe-recs API; #intraday-regime ↔ `/api/regime/*`; #trade-signals/#research-feed ↔ :3000 surfaces; `/agents`, `/signals`, `/pipeline status`, flash lookups ↔ dashboard reads.

## Dead handlers (delete or port consciously)

- **`rec:` position-recommendation buttons** (`bot.js:1787-1856`): handler + backend (`execute_recommendation.py` — a live-order path!) alive, but **no producer posts button components anywhere** — orphaned. If kept, port to a dashboard button.
- **`notifyStrategyReport` ✅/❌ react-to-approve** (`notifications.js:87-114`): zero callers AND no reaction handler for those emojis — doubly dead. `notifyEmergencyAlert`, `notifyPositionRecommendation` also caller-less.
- **IC approvals** (`ic_handler.js`): alive but `OPENCLAW_IC_GATE` unset AND no `ic-approvals` webhook registered — inert end-to-end.
- **`/approve {trade_id}` / `/reject {trade_id}`**: legacy `trades`-table lane; producers have no callers.
