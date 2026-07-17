# Spec — Wire strategist-ideator candidates into the saturday_brain coding path

**Date:** 2026-05-25
**Branch:** `feat/sp2-d-ideator-predicate` (builds on A's commit `daf29f3`, which fixed the producer side)
**Scope:** pre-existing bug discovered during SP-2 Phase D soak. The strategist-ideator
generates strategy ideas every Saturday, but they have **never** been routed to coding.

## Root cause (evidence-backed)

The ideator (`src/agent/prompts/subagents/strategist-ideator.md`) INSERTs each idea into
`research_candidates` with a **pre-filled `hunter_result_json`** (a complete strategy spec).
The only automatic consumer that codes candidates is `saturday_brain._hunt` (Phase 4) →
`runHunterFanout` → `_tier` (Phase 5) → `_code` (Phase 6). Three blockers stop ideator
candidates from ever entering that path:

1. **submitted_by mismatch** — `_hunt` Population-1 selects
   `submitted_by IN ('curator','curator_spotcheck','ideator')`; the ideator writes
   `submitted_by='strategist-ideator'`.
2. **pre-fill exclusion** — `_hunt` Population-1 requires
   `hunter_result_json IS NULL OR ::text IN ('null','{}')`; ideator candidates always
   carry a pre-filled spec, so they are excluded.
3. **kind not tagged** — `runHunterFanout`'s bypass (research-orchestrator.js:991) skips
   PaperHunter only for `kind='internal'`. The `kind` column defaults to `'paper'` and the
   prompt-as-written never set it, so 25/30 live rows are `kind='paper'`; even if selected,
   `_runPaperHunter` would fetch the fake `ideator://…` URL and reject the spec.

`processQueue`'s READY path (the other place that could code internal drafts) is **dead**:
it routes through `_runSubagent('researchjohn', …)` (line 422) but `researchjohn` is not in
`src/agent/config/models.js`, so the call rejects and `classification` stays empty.

Live data confirms (30 ideator rows): 25 `kind='paper'` + 5 `kind='internal'`, all
`submitted_by='strategist-ideator'`, all pre-filled, all `inferred_universe_filter=null`,
**0 ever coded**.

## The bypass machinery already exists

`runHunterFanout` (research-orchestrator.js:991-999) was explicitly built for
"MasterMindJohn pre-filled spec (e.g. ideator drafts)" — it skips PaperHunter and passes
the spec straight through with `_bypass:'kind_internal'`. `_tier` then tiers the bypass
result (no `rejection_reason_if_any`), and `_code` → `_codeFromQueue` → `_codeStrategy`
reads `strategySpec.inferred_universe_filter` (line 1047) and threads it to the coder.
**The producer and the `_hunt` selector were simply never aligned to this contract.**

## The fix

### Producer (already done by A's `daf29f3` — keep as-is)
- `strategist-ideator.md` INSERT now sets `kind='internal'`.
- §5 "Infer universe slice" section: pick one of the 12 real `CANDIDATE_PREDICATES` or null.
- Output spec JSON now carries `inferred_universe_filter`.
- `tests/test_ideator_predicate.py` — static prompt-contract checks.

### Selector (this task — `src/agent/curators/saturday_brain.js`)
Add **Population-1b** to `_hunt`, alongside the existing fresh + retry populations:

```sql
-- Pre-specced internal drafts (ideator / MasterMindJohn) — bypass PaperHunter.
-- Contract mirrors runHunterFanout's kind='internal' bypass (research-orchestrator.js:992).
-- Dedup on data_tier IS NULL: _tier stamps data_tier on every processed candidate
-- (saturday_brain.js:307), so a coded/tiered draft is not re-selected next cycle —
-- the bypass deliberately does NOT rewrite hunter_result_json, so the paper-path
-- "non-null hunter_result_json = already processed" marker does not apply here.
SELECT candidate_id::text AS candidate_id, 'internal' AS pop
  FROM research_candidates
 WHERE kind = 'internal'
   AND hunter_result_json IS NOT NULL
   AND hunter_result_json::text NOT IN ('null','{}')
   AND hunter_result_json->>'strategy_id' IS NOT NULL
   AND data_tier IS NULL
   AND status IN ('pending','processing')
 ORDER BY priority DESC, submitted_at DESC
 LIMIT $internalCap
```

- Concat into the combined fan-out list passed to `runHunterFanout` (which already
  bypasses PaperHunter for these). No change needed to `runHunterFanout`, `_tier`, `_code`.
- Bound `internalCap` so internal drafts never starve the fresh/retry paper budget
  (e.g. `Math.min(40, Math.max(0, maxFanout - fresh.length - stuck.length))`, computed after
  the fresh + stuck selections). Internal drafts should be additive, not displace papers.
- Keep the existing populations and their dedup semantics unchanged.

### Producer-prompt consistency (small `_ideate` edit)
The Phase-6.5 `_ideate` inline prompt (saturday_brain.js:377-380) instructs
`submitted_by='ideator'`, which conflicts with `strategist-ideator.md`'s
`submitted_by='strategist-ideator'`. Selection no longer depends on `submitted_by`
(Population-1b keys on `kind='internal'`), so reconcile the inline prompt to defer to the
subagent's own documented INSERT format rather than dictating a different `submitted_by`.

## Out of scope (deferred to operator)

- **Backfill of the 20 pending `kind='paper'` ideator rows.** Flipping them to
  `kind='internal'` would route ~25 un-vetted, pre-§5 ideas (`inferred_universe_filter=null`
  → default `sp500`) into one cycle's Tier-A coding (~$12.50). This is an independent
  operator decision; the forward fix works on day-1 new candidates. The 5 existing
  `kind='internal'` pending rows (data_tier IS NULL) ride along for free as a small,
  low-risk validation cohort.
- **Phase ordering.** `_ideate` (6.5) runs after `_hunt` (4), so ideas generated this run
  are coded next run — by design (a one-week research backlog). Not changing it here.

## Tests

- `tests/test_ideator_predicate.py` (A's — keep): prompt-contract static checks.
- **New JS test** (match existing `.test.js` node:test convention): assert `_hunt`'s
  Population-1b selects a pre-filled `kind='internal'` `data_tier IS NULL` pending row and
  excludes (a) the same row once `data_tier` is set, (b) `kind='paper'` ideator rows.
  Mock `_query` rather than hitting a live DB.

## Verification (post-implementation, done by controller against live DB)

Seed one `kind='internal'` pending row (minimal spec with `strategy_id`,
`inferred_universe_filter='sp500'`, `source_url='ideator://test-…'`) and confirm
`_hunt` → bypass → `_tier` → `_code` reaches strategycoder with the predicate threaded;
confirm `data_tier` gets stamped so the row is not re-selected.
