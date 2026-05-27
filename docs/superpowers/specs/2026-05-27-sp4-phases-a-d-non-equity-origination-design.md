# SP-4 Phases A–D — Non-Equity Research Origination (Design)

**Date:** 2026-05-27
**Status:** Design approved (operator: "looks good"). Plan + execution to follow in worktree `worktree-sp4-phases-a-d` (branched fresh from `origin/main` @ `1fc0e43`, the Phase 0 merge).
**Supersedes scoping in:** `docs/superpowers/specs/2026-05-27-sp4-weekly-research-uplift-design.md` §"Phase A/B/C/D" (that doc was the program decomposition; this is the consolidated single-sub-project design for A–D).

---

## 1. Goal

Teach the Saturday research **origination** stack (corpus curator + PaperHunter swarm + StrategyCoder + MasterMind reviewer) that the SP-2 broad universe and the SP-3/3.1/Phase-0 asset classes (`option`/`etp`/`crypto`) are in scope — so it can *originate* non-equity strategies end-to-end, not just equity-momentum.

This is **one cohesive sub-project**, not four independent ones: A–D are tightly-coupled increments to a single pipeline (paper → recognized → coded → reviewed), unified by a single new artifact, `inferred_instrument_class`. They get one spec, one plan (phase-grouped tasks), one merge.

## 2. Why this is feasible in one pass

The heavy infrastructure already exists on `main`:
- **`instrument_class` rails** (SP-3): `StrategyRecord.instrument_class` (default `equity`, silent-strip-safe `from_manifest`/`to_dict`), `VALID_INSTRUMENT_CLASSES={equity,option,etp,crypto,futures}`, `ROUTED_INSTRUMENT_CLASSES={equity,option,etp,crypto}`, the sizing dispatcher, and the class-aware backtest cost model.
- **Synthetic greeks options engine** (Phase 0): `src/backtest/options_backtest.py` + `synthetic_iv.py` + `vol_index.py` (`VALID_OPTION_UNDERLYINGS={SPY,SPX,^GSPC,QQQ,IWM}`), dispatched in `unified_backtest.run_backtest` only for `instrument_class='option'`.
- **Per-class promotion thresholds** (Phase 0): `lifecycle.py:PROMOTION_THRESHOLDS` = `equity`/`etp` 0.5/0.20, `option` 0.80/0.30, `crypto` 0.50/0.70 — **already enforced** at the candidate→live transition (`S_btc_momentum` promoted live under crypto thresholds 2026-05-26, proving the guard).
- **Reference strategies** for all three classes (`S_commodity_etp_momentum` etp candidate, `S_btc_momentum` crypto live, `S_short_straddle_vrp` option candidate) — proof the rails flow.

What remains is **prompt edits + a JS validation hook + code-level envelope validators + ingestion breadth + review-context** — far lighter than Phase 0. We are **not** running the 4–6h Saturday brain; we only edit its prompts. Dev spend is mostly cheap Sonnet subagents plus one bounded acceptance run.

## 3. Locked decisions

1. **Definition of done = BOTH** deterministic regression tests (on the code-level threading) **and** one bounded real run (acceptance).
2. **Class scope = all three** (`etp` + `option` + `crypto`), each behind a **readiness envelope** encoded as a guardrail.
3. **Scope width = maximal** — core spine + corpus recognition + ingestion breadth (arXiv categories, author watchlist) + crypto-column taxonomy + review-context.
4. **Proof class = `option` (index-vol)** — the bounded real run originates an index/ETF vol strategy (exercises the full new stack: recognition → envelope → greeks-engine backtest → 0.80/0.30 threshold), candidate-only, surfaced for OK before persist.
5. **Crypto-column reconciliation** — the `servers.json` crypto taxonomy is a **declarative availability map** (price-derived BTC/ETH OHLCV = *available*; `funding_rate`/`perp_oi`/`order_book` = *declared-but-unavailable*), so "maximal" never lets PaperHunter falsely pass a strategy needing data we cannot backtest.

## 4. Readiness envelopes (the guardrails)

Each class is originatable only within what we can actually backtest today. PaperHunter must accept inside the envelope and cleanly reject outside it; **code enforces** the machine-checkable parts.

| Class | In-envelope (accept) | Out-of-envelope (reject) | Enforcement |
|---|---|---|---|
| `etp` | Commodity/sector ETPs on generic `prices` (equity-like backtest) | Leveraged/inverse-decay ETPs requiring intraday-decay modeling | Prompt (no leverage-decay engine) |
| `option` | Index/ETF, ATM, near-term on `{SPY,SPX,^GSPC,QQQ,IWM}` | Single-name options; OTM-wing/skew strategies; non-listed underlyings | **Code:** orchestrator checks inferred underlying ∈ `VALID_OPTION_UNDERLYINGS` → demote/reject (`option_envelope_unsupported`) |
| `crypto` | BTC/ETH price-only signals (momentum/carry on existing daily bars) | Strategies needing `funding_rate`/`perp_oi`/`order_book` | **Code:** capability gate reads `servers.json` availability flags → reject |

## 5. Architecture — the `instrument_class` threading spine

Parallels SP-2 Phase D's `universe_filter_ref` flow exactly. **Prompts infer; code validates and enforces.**

```
PaperHunter (paperhunter.md §3/§7)
   └─ emits inferred_instrument_class ∈ {equity,option,etp,crypto} (default equity)
      + applies envelope-rejection clauses
        │
        ▼
research-orchestrator.js
   └─ _validateInferredClass(name)  ── gated OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT
        ├─ name ∈ VALID_INSTRUMENT_CLASSES ? keep : fall back to 'equity'
        ├─ if option: inferred underlying ∈ VALID_OPTION_UNDERLYINGS ? keep : reject/demote
        └─ thread validated class into coder context + queued strategy_spec
        │
        ▼
StrategyCoder (strategycoder.md)
   └─ writes "instrument_class": "<validated>" into the manifest entry
      + per-class code template (Signal direction, OptionSpec, universe_filter handling)
        │
        ▼
lifecycle.register()
   └─ persists instrument_class at mint (field already round-trips via from_manifest/to_dict)
      + optional defensive AST cross-check (warn on code/manifest mismatch)
        │
        ▼
lifecycle.transition() (candidate→live)
   └─ _promotion_threshold(rec.instrument_class) applies per-class min_sharpe/max_drawdown
      [ALREADY WIRED — Phase 0]
```

**Gate semantics:** `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT` default-OFF; absent treated as OFF (matching Phase D's `OPENCLAW_PHASE_D_PREDICATE_AT_MINT` convention). When OFF, every mint defaults to `equity` and the pipeline is **byte-identical** to today. VPS `.env` adds the line to activate after soak. **No schema migration** — `inferred_instrument_class` rides `research_candidates.hunter_result_json` (JSONB), `instrument_class` rides the existing manifest field.

## 6. Per-phase design

### Phase A — Ingestion breadth + corpus recognition

- **`src/ingestion/arxiv_discovery.py`** — add `q-fin.PR` (Pricing of Securities) and `q-fin.MF` (Mathematical Finance) to `CATEGORIES`. **Deliberately exclude `math.PR`** (probability) — too noisy for the implementability gate's ROI.
- **`src/ingestion/openalex_discovery.py`** — extend the author watchlist with options/vol + crypto researchers; add implied-vol / options-pricing concepts to the concept harvest. Venue list unchanged (SSRN/NBER already capture crypto working papers).
- **`src/agent/prompts/subagents/mastermind.md`** (corpus rating prompt) —
  - **Remove crypto from the "NEVER AVAILABLE" list** (stale post-SP-3.1). Reword: "BTC/ETH spot prices + vol indices are *available* (limited to those pairs); futures/FX remain unavailable."
  - Add **options heuristics**: strong-positive = index/ETF vol-premium strategies implementable via the synthetic greeks engine; strong-negative = exotic/structured/forex/single-name-wing/crypto-derivative.
  - Add `inferred_instrument_class` to the rating output JSON schema.
- **`src/agent/curators/mastermind.js`** — at promotion (high-bucket → `research_candidates`), apply **per-class corpus confidence floors** as a budget pre-filter: `option` ≥ 0.80, `crypto` ≥ 0.70, `equity`/`etp` ≥ 0.75 (current). These are *tunable heuristics* to avoid spending PaperHunter/backtest budget on candidates that will fail the authoritative lifecycle promotion gate — **not** the authoritative gate itself.
- **`src/agent/config/servers.json`** — add a **crypto-column availability taxonomy**: BTC/ETH OHLCV (price-derived) marked *available*; `funding_rate`, `perp_oi`, `order_book` marked *declared-but-unavailable*. The capability gate reads these flags.

### Phase B — PaperHunter recognition + envelope guardrails

- **`src/agent/prompts/subagents/paperhunter.md`** —
  - Add `inferred_instrument_class` to the §3 extraction schema and §7 output JSON (sits alongside `inferred_universe_filter`, independent of it).
  - **Inference rules:** `options_eod` in `data_requirements.required` OR `SELL_VOL`/`BUY_VOL` direction → `option`; commodity-ETP tickers (GLD/SLV/USO/…) or explicit ETP framing → `etp`; BTC/ETH spot + prices-only → `crypto`; else `equity` (default).
  - **Envelope-rejection clauses** (new §6-style self-rejection): option strategy not on an index/ETF ATM near-term underlying → reject `option_envelope_unsupported`; crypto strategy requiring a `servers.json`-unavailable column → reject via the existing capability gate.
- **`src/agent/research/research-orchestrator.js`** (owns `_validateInferredFilter` + `CANDIDATE_PREDICATES`) —
  - Add **`_validateInferredClass(name)`** parallel to `_validateInferredFilter`: validates against `VALID_INSTRUMENT_CLASSES` (via the same Python-subprocess pattern), gated on `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT`, falls back to `equity` on unknown.
  - **Code-level option-envelope enforcement:** when class resolves to `option`, verify the inferred underlying ∈ `VALID_OPTION_UNDERLYINGS` (import from Phase 0's `vol_index.py`); if not, demote to rejection. This is the deterministically-testable enforcement behind the prompt clause.
  - Thread the validated class into the StrategyCoder context + the queued `strategy_spec`.

### Phase C — StrategyCoder per-class templates

- **`src/agent/prompts/subagents/strategycoder.md`** —
  - Manifest-entry template gains `"instrument_class": "<validated value, default equity>"` with explicit rule (write the orchestrator-supplied class; default `equity` when unspecified — preserves backward compat).
  - New **per-class guidance** section: `option` → `SELL_VOL`/`BUY_VOL` directions + populate `Signal.option_spec` (the Phase-0 `OptionSpec` dataclass), universe typically `options_eligible_only` or null; `crypto` → `LONG`/`FLAT`, **no** `universe_filter` import (sp500 doesn't apply); `etp` → momentum on generic `prices`.
- **`src/strategies/lifecycle.py:register()`** — persist the orchestrator-validated `instrument_class` at mint (field already round-trips). Optional defensive AST cross-check: if the impl file carries a module-level class marker that disagrees with the manifest value, log a warning (non-fatal).

### Phase D — Review-awareness + the proof

- **`src/agent/curators/comprehensive_review.js`** + **`src/agent/curators/position_recommender.js`** — pass `instrument_class` + its per-class `PROMOTION_THRESHOLDS` into the Opus memo context, so a crypto/option strategy's metrics are judged against the right floor (e.g., option Sharpe judged vs 0.80, not equity's 0.50). Informational; does not change the deterministic sizing math.
- **Per-class threshold wiring** — already enforced (Phase 0); Phase D confirms via the deterministic test below.

## 7. Tests + proof (Definition of Done)

**Deterministic regression tests** (the gate — zero LLM spend, repeatable):
- JS: `_validateInferredClass` — valid class kept; unknown → `equity`; gate OFF → no-op (returns `equity`); option underlying outside `VALID_OPTION_UNDERLYINGS` → rejected.
- Python: `lifecycle.register()` persists `instrument_class` supplied in the spec; `from_manifest`/`to_dict` round-trip (extends `tests/test_lifecycle_instrument_class.py`); `_promotion_threshold` returns the right per-class dict; a candidate→live transition for each class applies the correct min_sharpe/max_drawdown.
- Python: option-underlying envelope validator (the shared function the orchestrator calls) — accepts `{SPY,SPX,^GSPC,QQQ,IWM}`, rejects single-name/OTM.

**Bounded real run** (acceptance — surfaced for OK before persist):
- Run `paperhunter` + `strategycoder` (Sonnet) on a curated index-vol option paper with the gate ON **in the worktree** (prod `.env` untouched). Expected: a candidate-only strategy with `instrument_class=option` in its manifest entry, envelope passed, backtest executed via the synthetic greeks engine, and the 0.80/0.30 threshold applied at the (blocked, candidate-only) promotion check. Inspect + report; do not promote.

## 8. Gating & safety

- `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT` default-OFF → equity origination **byte-identical** when off.
- All prompt edits additive; no existing strategy's behavior changes.
- No schema migration.
- Worktree-isolated; `main` untouched until a deliberate, surfaced merge.
- Integrity-manifest regen on the VPS for any tracked, manifest-covered prompt file edited (`./scripts/regen-integrity-manifest.sh`; do **not** commit the manifest).

## 9. Out of scope / deferred

- Single-name + OTM-wing options origination (until real option-chain history accrues — Phase 0 deferral stands).
- Actual crypto microstructure data (`funding_rate`/`perp_oi`/`order_book`) — taxonomy declares them unavailable; ingestion of that data is a separate future effort.
- `futures` recognition (still reserved/unrouted in the rails).
- Live execution of any newly-originated strategy (all proofs are candidate-only).

## 10. Grounding note (for plan-writing)

Before dispatching subagents, grep-verify against live source: exact `research-orchestrator.js` path + `_validateInferredFilter`/`CANDIDATE_PREDICATES` signatures; `mastermind.js` promotion-bucket code; `arxiv_discovery.py:CATEGORIES` list; `servers.json` covered-columns shape; `lifecycle.register()` signature + the Phase-D `_detect_module_predicate` pattern; `vol_index.py:VALID_OPTION_UNDERLYINGS` import path; `paperhunter.md` §3/§5/§6/§7 line anchors. (Per `feedback_spec_plan_codebase_grounding`.)
