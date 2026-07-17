# TradingAgents Imports — Design Spec

**Date:** 2026-05-20
**Status:** Design — awaiting operator approval before plan-writing
**Companion docs:** [ARCHITECTURE.md](../../../ARCHITECTURE.md) · [PIPELINE.md](../../../PIPELINE.md) · [CLAUDE.md](../../../CLAUDE.md)

---

## 1. Context & motivation

Inventory pass on the open-source [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) repo surfaced 16 candidate ideas. After operator review, three were selected for design and implementation:

- **B3** — Per-node deep/quick LLM tiering, centralized in `subagent-types.json`
- **D1** — Reddit + StockTwits + FinBERT sentiment ingestion, wired into the TradeJohn confirmer as a gate-only input (no boost multiplier)
- **F3** — Three-way Sonnet critic loop (aggressive / conservative / neutral) on Mastermind's weekly strategy memos, followed by a Mastermind (Opus) synthesis pass that produces adjusted sizing recommendations

The remaining ideas were either deferred (E1 — LangGraph migration), scrapped (F2 — pending/resolved decision ledger), or judged of insufficient value (everything else).

These three changes share a substrate: B3 provides the model-tier primitive that F3 explicitly uses. D1 is independent. Implementation can ship in any order, but B3 first lets F3 import the tier configuration directly.

---

## 2. Summary

| ID | Change | Surface area | Risk | Cost delta |
|----|---|---|---|---|
| B3 | Per-node model resolution helper + config schema | All curators; one new helper file | Low (additive) | Net **down** ~15-25% on non-judge node spend |
| D1 | Daily sentiment substrate + confirmer enrichment | New ingestion step; new Postgres table; new parquet; confirmer prompt rewrite | Low-medium (gate-only output preserved) | ~$0.01-0.03 / cycle confirmer; zero LLM elsewhere |
| F3 | Saturday critique loop + Mastermind synthesis | `comprehensive_review.js` extension; new `mastermind --mode synthesize`; new tables | Low (Saturday-only, additive) | ~$70/month |

---

## 3. B3 — Centralized per-node model tiering

### 3.1 Goal

Lift model selection from "one model per agent" to "one model per *node* within an agent's graph", centralized in `src/agent/config/subagent-types.json`. Zero per-call overrides scattered through curator code.

### 3.2 Schema change

`subagent-types.json` gains two optional fields per subagent: `model_tiers` (named-tier map) and per-mode `node_models` (node-name → tier-or-model). Both are optional; subagents without them resolve to `.model` exactly as today.

```jsonc
{
  "mastermind": {
    "model": "opus-4-7",
    "model_tiers": {
      "judge":       "opus-4-7",
      "synthesizer": "opus-4-7",
      "debator":     "sonnet-4-6",
      "extractor":   "sonnet-4-6"
    },
    "modes": {
      "comprehensive-review": {
        "node_models": { "memo_writer": "judge" }
      },
      "critique": {
        "node_models": {
          "aggressive_critic":   "debator",
          "conservative_critic": "debator",
          "neutral_critic":      "debator"
        }
      },
      "synthesize": {
        "node_models": { "synthesizer": "synthesizer" }
      }
    }
  }
}
```

### 3.3 Resolution helper

New file `src/agent/config/resolve_model.js`:

```js
// Resolution order:
//   subagent.modes[mode].node_models[nodeName]   →  string OR tier key
//   subagent.model_tiers[tier]                   →  model id
//   subagent.model                               →  default
function resolveModel(subagentType, mode, nodeName) → modelId
```

Every curator call site replaces the literal `MODELS.primary` / `MODELS.orchestrator` with `resolveModel('mastermind', mode, 'this_node')`. Five files touched: `mastermind.js`, `comprehensive_review.js`, `position_recommender.js`, `paper_expansion_ingestor.js`, `saturday_brain.js`.

### 3.4 Migration path

Subagents without `model_tiers` / `node_models` resolve to `.model` (default) — identical to today. Roll out one subagent at a time. Invocation logs include one new line per call: `[agent] resolved sonnet-4-6 for node=aggressive_critic`.

### 3.5 Error handling

- Unknown `nodeName` → fall back to `model_tiers.debator` if defined, else `.model`, with `console.warn` so config drift surfaces.
- Unknown tier name in `model_tiers` → ignore tier, fall back to `.model`. Same warn.

### 3.6 Testing

- `tests/test_resolve_model.js` — six cases: full resolution chain, two fallback paths, two unknown-key paths, override-wins-over-default.
- Smoke: run one Mastermind comprehensive-review with `TRACE=1`, verify the resolved model per node matches config.

### 3.7 Cost impact

Today's Mastermind Saturday spend roughly $90/week. With F3's critic additions but per-node tiering, projected ~$95/week (vs. ~$100/week without tiering). Net ~15-25% lower per-node spend on non-judge work across the system.

---

## 4. D1 — Sentiment ingestion + confirmer wiring

### 4.1 Goal

Build a continuous `(ticker, date)` sentiment substrate fed by Reddit + StockTwits + FinBERT-over-news. Wire it into the TradeJohn confirmer's per-ticker prompt context. Confirmer output stays strict `keep|cancel` — no multiplier, no boost. Sentiment functions as veto-only input.

### 4.2 Components

| # | Component | File / artifact |
|---|---|---|
| 1 | Social scraper | new `scripts/fetch_social_sentiment.py` (Reddit r/wsb, r/stocks, r/investing + StockTwits sparse) |
| 2 | News sentiment scorer | new `scripts/score_news_sentiment.py` (today's `market_news` → FinBERT on `127.0.0.1:7872`) |
| 3 | Storage | new Postgres table `ticker_sentiment_daily` + new master parquet `data/master/sentiment.parquet` (append-only) |
| 4 | Handoff enrichment | `src/execution/trade_handoff_builder.py` injects per-ticker sentiment block into each proposal |
| 5 | Confirmer prompt | `src/agent/prompts/subagents/tradejohn-confirmer.md` rewrite; minor parser updates in `src/execution/tradejohn_confirmer.py` |

### 4.3 Universe handling (auto-expand)

Universe resolved at runtime as the union of:
1. SP500 + universe ETFs from `market_universe`
2. Currently held tickers from `execution_positions`
3. Any ticker referenced by any live/candidate strategy in `manifest.json`
4. Operator watchlists

Single helper `src/ingestion/resolve_sentiment_universe.py::current_universe()` so the policy lives in one place. No ticker hardcoding anywhere.

### 4.4 Pipeline integration

New step `sentiment` in `pipeline_orchestrator.py` STEPS, between `collect` and `signals`:

```
collect → sentiment → signals → ic_gate → handoff → trade → alpaca → reconcile → report → ...
```

Placing before `signals` allows future strategies to consume sentiment as a feature without re-fetching.

### 4.5 Data flow

```
[r/wsb, r/stocks, r/investing]─┐
                               ├─▶ parse $TICKER mentions ──┐
[StockTwits sparse]────────────┘                            │
                                                             ├──▶ ticker_sentiment_daily ──▶ sentiment.parquet (append)
[market_news (today)]──▶ FinBERT (:7872) ──▶ aggregate ─────┘                  │
                                                                               │
                                                          trade_handoff_builder
                                                          injects per-ticker block
                                                                               │
                                                          tradejohn_confirmer
                                                          reads block → keep | cancel
```

### 4.6 Schema (new migration `103_ticker_sentiment_daily.sql`)

```sql
CREATE TABLE ticker_sentiment_daily (
  ticker TEXT NOT NULL,
  date   DATE NOT NULL,
  -- social (Reddit + StockTwits aggregate)
  social_posts_24h        INT     DEFAULT 0,
  social_bull_ratio       NUMERIC,
  social_bear_ratio       NUMERIC,
  social_unique_authors   INT     DEFAULT 0,
  social_top_themes       JSONB,
  -- news (Tavily-fed, FinBERT-scored)
  news_count_24h          INT     DEFAULT 0,
  news_finbert_pos        NUMERIC,
  news_finbert_neu        NUMERIC,
  news_finbert_neg        NUMERIC,
  news_mean_score         NUMERIC,  -- signed: +1 fully positive ... -1 fully negative
  news_top_headlines      JSONB,    -- top 3 by |polarity|
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, date)
);
CREATE INDEX idx_sentiment_date ON ticker_sentiment_daily(date);
```

Master parquet `data/master/sentiment.parquet` mirrors the same column shape. Append-only per the never-delete invariant.

### 4.7 Confirmer prompt addendum

New section appended to `src/agent/prompts/subagents/tradejohn-confirmer.md`:

```markdown
## Sentiment & News Inputs

Each ticker carries: social_posts_24h, social_bull_ratio, social_bear_ratio,
news_finbert_pos/neu/neg, news_mean_score, news_top_headlines.

CANCEL when ANY of:
  1. news_top_headlines contains a hard-veto event (fraud, FDA rejection,
     bankruptcy, regulatory action, restatement, CEO departure for cause,
     catastrophic operational failure)
  2. news_mean_score ≤ -0.5 AND signal direction is LONG
  3. news_mean_score ≥ +0.5 AND signal direction is SHORT
  4. social_bear_ratio ≥ 0.7 AND social_posts_24h ≥ 50 AND signal is LONG
  5. social_bull_ratio ≥ 0.7 AND social_posts_24h ≥ 50 AND signal is SHORT

KEEP otherwise. Default is keep.

DO NOT cancel for: earnings (handled separately), sector moves, macro news,
broad-market sentiment, or low-volume social (posts_24h < 50 = noise).
```

### 4.8 Error handling

- Reddit / StockTwits HTTP error → skip that source, continue with what we have. `social_posts_24h = 0` → confirmer treats as neutral.
- FinBERT service down → news polarity fields written as NULL; confirmer treats as neutral, can still cancel on `news_top_headlines` text-match (rule 1).
- Universe-lookup failure → abort step (fail loud — sentiment without universe is meaningless).
- Step timeout (orchestrator 20 min cap) → kill step, pipeline continues without sentiment for today's cycle. Logged to `#data-alerts`.

### 4.9 Cost & latency

- Pure scraping + local FinBERT — **zero LLM cost** for ingestion.
- Step latency: ~7-12 min for full universe.
- Confirmer prompt grows ~1-1.5K tokens (sentiment block injected per proposal — typically ~40-60 tickers per cycle, after the sharpe-cadence gate). Confirmer cost ~$0.05/cycle today → expected ~$0.06-0.08/cycle.

### 4.10 Testing

- `tests/test_social_sentiment_aggregation.py` — canned Reddit + StockTwits payloads → assert counts, bull/bear ratios, theme extraction.
- `tests/test_news_finbert_aggregation.py` — canned `market_news` rows + mocked FinBERT → assert rollup math.
- `tests/test_confirmer_sentiment_veto.py` — table-driven: feed proposals + sentiment, assert keep/cancel matches each prompt rule (5 cancel rules + 3 keep cases + boundary cases at `posts_24h = 49` vs `50`, `news_mean_score = -0.49` vs `-0.51`).
- Smoke: run `sentiment` step against today's universe; verify `ticker_sentiment_daily` populated, parquet appended, confirmer log shows the sentiment block injected.

---

## 5. F3 — Mastermind self-critique loop

### 5.1 Goal

Insert a 3-way critique pass between Mastermind's Saturday memo writing (18:00 ET) and position recommendation derivation (19:00 ET). Three Sonnet critics attack each eligible memo from different angles; Mastermind (Opus) re-enters as the synthesizer, reads original memo + 3 critiques + recent realized P&L, and produces ADJUSTED sizing recommendations.

### 5.2 Saturday flow (delta)

```
Sat 18:00 ET   comprehensive_review.js   →  strategy_memos (Mastermind Opus, original)
Sat 18:30 ET   ← NEW: critique fan-out   →  strategy_memo_critiques (3 Sonnet critics, parallel)
Sat 19:00 ET   position_recommender.js   →  Mastermind Opus synthesizer pass
                                          →  strategy_synthesis (adjusted recs)
                                          →  strategy_sizing_recommendations  ← fed from synthesis
                                          →  #position-recommendations Discord post (mentions deltas)
```

### 5.3 Activity filter (closed-trades-only)

A strategy is critique-eligible if and only if it has **≥1 closed trade in the last 7 calendar days**:

```sql
SELECT DISTINCT strategy_id
FROM signal_pnl
WHERE exit_date >= CURRENT_DATE - INTERVAL '7 days'
  AND exit_date IS NOT NULL
```

Open positions alone do **not** trigger critique — strategies must be allowed to complete their hold-period cadence before being judged. Multi-week-cadence strategies that haven't realized anything are skipped this week.

Helper: `src/agent/curators/_critique_eligibility.js::filter()` — single query, single source of truth. Expected ~15-30 of 51 live strategies typically eligible per week.

### 5.4 The three critics

Each critic is a Sonnet call (B3-resolved `debator` tier) with a focused prompt. All three see the same input (original memo + last 30d realized P&L for this strategy + current open positions for context). Run in parallel. Hard $0.10 budget per critic invocation.

| Critic | Prompt mandate | Output |
|---|---|---|
| **Aggressive** | "The memo is too timid. Find missed alpha. Argue for larger sizes, longer holds, or opening shorts where avoided. Cite specific trades the memo recommended trimming where realized data suggests adding." | `critique_text`, `cited_metrics{}` |
| **Conservative** | "The memo is too aggressive. Find tail risks underweighted. Argue for tighter stops, smaller sizes, regime-mismatch flags. Cite specific drawdowns or near-misses." | same shape |
| **Neutral** | "The memo may contain specific factual or quantitative errors. Find inconsistencies between the memo's claims and the realized P&L numbers." | same shape |

Prompts live at `src/agent/prompts/critics/{aggressive,conservative,neutral}_critic.md`.

### 5.5 The synthesizer (Mastermind, Opus)

New mode: `mastermind --mode synthesize`. New prompt: `src/agent/prompts/subagents/mastermind-synthesizer.md`. New service unit: `docs/mastermind-synthesize.{service,timer}` firing Sat 19:00 ET (replaces the deterministic position_recommender invocation at that slot — see §5.7).

Inputs per strategy:
- Original memo from `strategy_memos`
- 3 critique texts from `strategy_memo_critiques`
- Last 30d realized P&L summary from `signal_pnl`
- Current open positions
- Last cycle's `strategy_sizing_recommendations` row

Output schema (structured JSON, enforced by parser):

```json
{
  "strategy_id": "S9_dual_momentum",
  "adjusted_recommended_size_pct": 0.024,
  "original_recommended_size_pct": 0.030,
  "adjustment_reason": "Conservative critic flagged that 3 of last 5 closed trades had >2% drawdown in HIGH_VOL regime; original memo did not account for this. Reducing size by 20%.",
  "critics_accepted": ["conservative"],
  "critics_rejected": [
    {"critic": "aggressive", "reason": "argues for size boost but cites only the 2 winning trades, ignores the 3 losing trades"},
    {"critic": "neutral", "reason": "raised stylistic concerns, no quantitative inconsistency identified"}
  ]
}
```

Mandatory output rules (enforced in prompt + structured-output parser):
- MUST include explicit accept/reject for each of the 3 critics with reasoning
- MUST cite ≥1 numeric (P&L %, drawdown, win rate, etc.) for any adjustment
- If no critic delivers a quantitatively-justified argument, `adjusted_recommended_size_pct = original_recommended_size_pct` (no-op)

### 5.6 New schemas (migration `104_memo_critiques.sql`)

```sql
CREATE TABLE strategy_memo_critiques (
  id           BIGSERIAL PRIMARY KEY,
  strategy_id  TEXT NOT NULL,
  week_of      DATE NOT NULL,
  critic_role  TEXT NOT NULL,  -- 'aggressive' | 'conservative' | 'neutral'
  critique_text TEXT NOT NULL,
  cited_metrics JSONB,
  cost_usd     NUMERIC,
  duration_sec NUMERIC,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of, critic_role)
);

CREATE TABLE strategy_synthesis (
  id              BIGSERIAL PRIMARY KEY,
  strategy_id     TEXT NOT NULL,
  week_of         DATE NOT NULL,
  synthesizer_text TEXT NOT NULL,
  original_recommended_size_pct  NUMERIC,
  adjusted_recommended_size_pct  NUMERIC,
  adjustment_reason TEXT,
  critics_accepted JSONB,
  critics_rejected JSONB,
  cost_usd        NUMERIC,
  generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of)
);
```

### 5.7 `position_recommender.js` modification

Currently deterministic — no LLM. Now becomes the orchestrator of the synthesizer pass:
1. Pull eligible strategies via `_critique_eligibility.filter()`
2. For each, call Mastermind synthesizer (via `run_mastermind.js --mode synthesize`)
3. Persist results to `strategy_synthesis`
4. For strategies NOT eligible: copy original memo's `recommended_size_pct` straight through into `strategy_sizing_recommendations` (today's behavior preserved)
5. For eligible strategies: source `strategy_sizing_recommendations.recommended_size_pct` from `strategy_synthesis.adjusted_recommended_size_pct`
6. Post consolidated `#position-recommendations` summary showing deltas (e.g., "S9 size 3.0% → 2.4%, conservative critic accepted")

### 5.8 Error handling

- Any individual critic LLM error → log + continue. Synthesizer sees only critics that succeeded. The synthesizer prompt instructs handling 1, 2, or 3 critics gracefully.
- All 3 critics fail for a strategy → synthesizer step skipped for that strategy; `strategy_synthesis` row inserted with `adjustment_reason='ALL_CRITICS_FAILED, defaulted to original'`, `adjusted = original`. Audit-visible no-op.
- Synthesizer LLM error → same fallback: original sizing flows through. Posted to `#data-alerts` since unusual.
- Activity filter returns empty list (quiet week) → entire critique step short-circuits; original memos flow straight to `position_recommender` (today's behavior).

### 5.9 Cost & latency

Estimates assume the typical ~15-30 eligible strategies/week per §5.3. Modeling at the midpoint (~20):

- Critics: ~20 strategies × 3 critics × ~$0.05 (Sonnet) ≈ **$3/week** (range $2-5)
- Synthesizer: ~20 strategies × ~$0.30 (Opus) ≈ **$6/week** (range $4-9)
- Total: **~$9-14/week ≈ $40-60/month** added to weekly research spend. Lower than the initial $70/month estimate because the closed-trades-only filter is stricter than the original "open OR closed" criterion.
- Latency: critics fan out in parallel ≈ 30-60s wall clock; synthesizer sequential ~60s/strategy × 20 strategies ≈ 20 min. Fits in the 18:30 → 19:00 window with buffer even at the upper bound (30 strategies × 60s = 30 min hits the window edge — acceptable).

### 5.10 Testing

- `tests/test_critique_eligibility.js` — fixtures with various activity profiles → assert correct strategies selected. Boundary cases: exactly 7 days ago, exactly 0 closed trades, strategies with only open positions (must be excluded).
- `tests/test_critique_fanout.js` — mock 3 Sonnet calls per strategy, assert all invoked, all rows persisted.
- `tests/test_synthesizer_acceptance_rules.js` — table-driven: feed canned (memo + 3 critiques + P&L) → assert structured output includes explicit accept/reject for each critic + numeric citation when adjusting.
- `tests/test_synthesizer_fallback.js` — assert all-critics-fail → `adjusted = original`, audit row present with reason.
- `tests/test_position_recommender_reads_synthesis.js` — assert `strategy_sizing_recommendations` is sourced from `strategy_synthesis` for eligible strategies, `strategy_memos` otherwise.
- Smoke: full Saturday dry run on a 2-strategy subset; verify Discord post mentions deltas.

---

## 6. Cross-cutting concerns

### 6.1 Rollout order

| # | Phase | Dependencies |
|---|---|---|
| 1 | **B3** — config primitive + helper + test | None |
| 2 | **D1** — ingestion + storage (sentiment step ships in shadow mode, confirmer NOT yet reading) | None (independent of B3) |
| 3 | **D1** — confirmer prompt rewrite + handoff enrichment + cutover | D1 phase 2 |
| 4 | **F3** — eligibility filter + critic prompts + parallel fan-out | B3 (uses tier resolution) |
| 5 | **F3** — synthesizer mode + position_recommender modification | F3 phase 4 |

D1 phase 2 ships with sentiment data populated but confirmer untouched. After ≥1 week of observed data in `ticker_sentiment_daily`, operator reviews and enables the confirmer prompt change (phase 3). This avoids surprise behavior changes.

**Plan decomposition note for writing-plans:** the three features can either be packaged as one phased plan or as three independent plans (B3 / D1 / F3) since their substrates barely touch. Recommendation: **three independent plans**. B3 ships small and standalone; D1 ships in two sub-phases (ingestion shadow → confirmer cutover); F3 ships once B3 is live. This keeps each plan tightly scoped and reviewable.

### 6.2 Kill switches

Every new behavior is introduced behind a default-OFF env flag (all four flags are net-new and ship with this work — none exist today). Flipped only after the corresponding shadow / observation window:

- `OPENCLAW_SENTIMENT_INGEST=1` — runs the new `sentiment` orchestrator step (else the step is skipped in `pipeline_orchestrator.py`)
- `OPENCLAW_CONFIRMER_SENTIMENT=1` — confirmer reads the sentiment block (else the prompt addendum is suppressed at build time, confirmer behavior unchanged)
- `OPENCLAW_MEMO_CRITIQUE=1` — Saturday flow inserts critique + synthesizer passes (else today's deterministic position_recommender runs unchanged)
- `OPENCLAW_MODEL_TIERING=1` — `resolveModel()` honors `model_tiers` / `node_models` (else returns `.model` default everywhere — pre-B3 behavior)

### 6.3 Dependency graph

```
B3 (config primitive) ──────────────┐
                                    ▼
                                  F3 (uses tier resolution for critic/synth models)

D1 (ingestion + confirmer enrichment) ─── independent ─── ships in parallel
```

### 6.4 Observability

- New Discord channel: none. Sentiment alerts surface in `#data-alerts`; critique deltas surface in existing `#position-recommendations`.
- Dashboard: optional Tier-3 addition — sentiment trend per ticker on the portfolio page, critique history per strategy on the strategies page. Not in scope for this spec.
- Logs: each B3 resolution call logs `[agent] resolved <model> for node=<name>` at INFO; each critic/synthesizer call logs cost + duration to `subagent_costs`.

---

## 7. Out of scope

Explicitly NOT addressed by this design:

- **E1 — LangGraph migration of pipeline_orchestrator.** Deferred to a separate brainstorming + spec cycle. Current Redis-based checkpoint substrate continues unchanged.
- **F2 — Pending → Resolved decision ledger.** Scrapped during scoping. No `decision_reflections` table.
- **D1 Half B — Confirmer multiplier / boost.** Scrapped. Confirmer output stays strict `keep|cancel`. Sentiment functions only as gate-input.
- **Per-node model resolution outside Mastermind.** B3 applies to all subagents in principle, but only Mastermind's modes ship configured in this iteration; other subagents continue using `.model` default. PaperHunter, StrategyCoder, BotJohn, TradeJohn-confirmer tier configurations are future work.

---

## 8. Resolved questions

Captured during brainstorming for the record:

| Question | Resolution |
|---|---|
| Model override location: config vs per-call | **Config** (`subagent-types.json`). Per-call would drift. |
| Sentiment universe scope | **Full universe**, auto-expanding via runtime resolver. |
| TradeJohn confirmer expansion to boost | **No** — gate-only. Output stays `keep|cancel`. |
| E1 implementation scope | **Defer entirely** — separate future spec. |
| F2 (decision ledger) | **Scrap** — no new ledger. |
| F3 synthesizer role | **Mastermind himself** (Opus), reading original memo + 3 critiques (both available by default). |
| F3 activity filter | **≥1 closed trade in last 7 days only** (open positions excluded — strategies allowed to reach cadence first). |

---

## 9. Open questions for plan-writing

These can be settled during writing-plans without re-opening this spec:

- Exact pagination + throttle settings for Reddit scraper
- Whether `_critique_eligibility.filter()` should run inside `comprehensive_review.js` (writes memos for all 51) or inside `position_recommender.js` (only the eligible subset). Recommend: memos always written (preserves audit trail), critique applied only to eligible subset.
- Whether the synthesizer prompt should explicitly see the previous week's synthesizer output (continuity) or only this week's memos + critiques (clean slate). Recommend: clean slate; previous synthesis is already reflected in this week's open positions and last-week P&L.

---

*End of spec.*
