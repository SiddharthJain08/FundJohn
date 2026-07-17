# SP-4 — Weekly Research Uplift (program decomposition)

**Date:** 2026-05-27
**Status:** Brainstormed + decomposed. Phase 0 spec written (`2026-05-27-sp4-phase-0-greeks-engine-design.md`). Phases A–D each get their own brainstorm → spec → plan → execute cycle.
**Depends on:** SP-2 (universe, A→D live), SP-3 (asset-class rails, live), SP-3.1 (crypto, live). All three complete.

---

## 1. Goal

Teach the Saturday/Sunday research stack (corpus curator + PaperHunter swarm + StrategyCoder + MasterMind reviewer) that the broader SP-2 universe and the new SP-3/3.1 asset classes (**options, etp, crypto**) are in scope — so it can **originate non-equity strategies end-to-end**, not just equity-momentum.

Today the stack skews equity-momentum at every gate: PaperHunter's implementability gate, StrategyCoder's templates, MasterMind's corpus prompt, and the promotion thresholds. SP-4 lifts each so a paper proposing (say) a vol-risk-premium options strategy or a crypto-carry strategy flows from corpus → candidate → coded → backtested → reviewed without manual intervention.

## 2. Decomposition

One phase at a time. Each mirrors the SP-2 / SP-3.1 phasing-by-pipeline-stage that worked.

| Phase | Name | Shape | Depends on |
|---|---|---|---|
| **0** | **Greeks engine** | Synthetic Black-Scholes options backtest + greeks-aware sizing + threshold calibration + reference strategy. *(Spec written.)* | SP-3 rails |
| **A** | Ingestion & recognition | arXiv category expansion (q-fin.PR derivatives, math.PR vol models); MasterMind `mode=corpus` prompt recognizes non-equity-momentum candidates instead of skewing equity. | — |
| **B** | PaperHunter uplift | Implementability gate accepts options/crypto/commodity/etp + broad-universe papers; infer `instrument_class` at mint (parallels SP-2 Phase D predicate-at-mint, rides `hunter_result_json`); universe-slice-fit check ("does this fit a buildable SP-2 slice?"). | Phase 0 (signal contract), A |
| **C** | StrategyCoder templates + proof | Per-asset-class scaffolds emitting correct `instrument_class` + `.requirements.json` + `registry.py` mapping + (for options) the `option_spec` signal contract from Phase 0; prove PaperHunter→Coder→backtest for ≥1 non-equity archetype originated from a real paper. | Phase 0, B |
| **D** | Calibration + review/recs awareness | Wire per-class thresholds into StrategyCoder + comprehensive-review; verify Sat-18:00 comprehensive-review + Sat-19:00 position-recs are asset-class-aware (sizing/review for non-equity). | Phase 0, C |

### Why this order
- **Phase 0 first** because everything downstream needs a *trustworthy* options backtest to evaluate originated option strategies, and because Phase 0 settles the `option_spec` signal contract that Phases B/C depend on.
- **A before B** because PaperHunter can't accept what the corpus never surfaces.
- **C after B** because templates consume the `instrument_class` + universe-fit that B infers.
- **D last** because calibration/review-awareness validates the whole originated-strategy lifecycle.

## 3. Cross-phase concerns (set in Phase 0, honored by all)

- **`option_spec` signal contract.** Phase 0 defines a backward-compatible optional `option_spec` on the strategy signal (underlying, right, strike rule, DTE, structure, hedge). It is the API boundary between research-origination (Phases B/C emit it) and execution/backtest (Phase 0 consumes it; a later live-execution phase reuses it). Settling it in Phase 0 prevents a Phase C redesign.
- **`instrument_class` threading.** Already top-level on `StrategyRecord` (SP-3), silent-strip-safe. Every phase emits/propagates it; no phase reintroduces an equity-only assumption.
- **Backtest↔live parity.** The contract-selection + greeks-sizing logic is written once so the synthetic backtest and (future) live executor agree by construction.

## 4. Locked decisions (from the 2026-05-27 brainstorm)

1. **Options are in-scope for SP-4** (not deferred to a sibling SP-3.2) — as **Phase 0**.
2. **Options backtest = synthetic Black-Scholes engine** (`py_vollib`) from 10y underlying history, because real options history is only ~7 weeks (2026-04-08→) — too short for a meaningful long-horizon Sharpe/MaxDD.
3. **IV model = realized-vol × VRP factor, calibrated** against the 7-week real-IV overlap (+ VIX for index options).
4. **Archetypes = single-leg + delta-neutral** (income/overwriting + volatility-risk-premium). Not full multi-leg (YAGNI).
5. **Calibration scope = option thresholds + crypto's open `min_sharpe`** placeholder (`TODO(SP-3.2)` at `lifecycle.py:104`).
6. **Reference strategy = short-straddle VRP** (delta-hedged), to exercise the full engine.

## 5. Open questions deferred to each phase's own brainstorm

- **A:** which arXiv categories exactly (q-fin.PR / math.PR / others), and how `mode=corpus` rating prompt changes (prompt-only vs scored features).
- **B:** prompt-level vs structural change to the implementability gate; how `instrument_class` inference is validated (whitelist like SP-2 Phase D); cost of widening the swarm.
- **C:** template mechanism (string scaffold vs a `strategy-template` tool); how many archetype templates.
- **D:** asset-class-aware sizing in `position_recommender.py` + `comprehensive_review.js`; per-class threshold surfacing.
- **Cross-cutting:** **live options execution** (real Alpaca chain contract selection + option order routing) is OUT of every phase here — needed only when an option strategy is *promoted live*. It reuses Phase 0's `option_spec`. Scope it as a fast-follow when the first option strategy clears promotion.

## 6. Cost / time constraints (carry into every phase)

- The Saturday brain is already a 4–6h job; Opus 4.7 1M passes are ~$8/call. Broader corpus = more passes. Confirm LLM-budget headroom before heavy subagent cycles (Phases A/B/C/D); Phase 0 is mostly deterministic Python (cheap).
- ~52 live + ~58 candidate strategies today; new classes add dozens more — watch corpus/review fan-out cost in D.

## 7. Operating constraints (from the handoff)

Live VPS, real paper money. Surface before any merge/deploy; surface paper-order/live ops before firing. NEVER delete from master DB (append-only). Never `git add -A` / never commit secrets. psql NOT installed — migrations via psycopg2, verify (non-idempotent `migrate()` wart). Worktree: symlink `data/master`; grep `.env`, don't source it.
