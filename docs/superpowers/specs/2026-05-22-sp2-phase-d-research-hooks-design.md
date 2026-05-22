# SP-2 Phase D: Research-Hooks for Predicate Emission — Design

**Spec date:** 2026-05-22
**Author:** BotJohn (extension of SP-2 umbrella)
**Status:** Pending operator review (cannot start before Phase C ships its first adoption cycle — at least 5 strategies must be on explicit predicates so Phase D has a precedent to follow)
**Parent:** `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.4
**Predecessors:** Phase A (PR #8), Phase B, Phase C
**Branch:** `feat/sp2-phase-d-research-hooks`

---

## 1. Context

Phases A–C migrate existing strategies onto explicit predicates. Phase D closes the loop on the *minting* side: when PaperHunter extracts a strategy spec from a paper and StrategyCoder turns it into a Python file, the resulting strategy emits a universe predicate at creation time — not as an afterthought waiting for Phase C's Saturday cycle to discover it 28 days later.

The mechanism: add a third PaperHunter gate that asks "does this paper imply a specific universe slice?" and writes `inferred_universe_filter` into the strategy spec JSON. StrategyCoder reads that field at code-emission time and either pastes the appropriate `from src.strategies.universe_default import <slice> as universe_filter` line into the strategy module OR leaves it absent (→ defaults to `sp500`). Lifecycle CLI propagates the choice into `manifest.metadata.universe_filter_ref` when the strategy is staged.

### 1.1 Anchored facts (2026-05-22)

- PaperHunter prompt (`src/agent/prompts/subagents/paperhunter.md`) has 2 self-rejection gates: `duplicate_fingerprint`, `capability_gap`. Phase D does NOT add a *rejection* gate; it adds an *extraction* field. Previously-removed gates (`non_deterministic`, `overfitting_risk`) are not relevant here.
- PaperHunter output is a JSON blob written to `research_candidates.hunter_result_json` (JSONB column from migration 025). Schema is free-form per the prompt; Phase D adds one optional top-level key: `inferred_universe_filter: <string from CANDIDATE_PREDICATES or null>`.
- StrategyCoder prompt (`src/agent/prompts/subagents/strategycoder.md`) describes a strict class contract (`BaseStrategy.generate_signals`). The current template does not mention `universe_filter`. Phase D extends the prompt to OPTIONALLY paste a top-level `from ... import ... as universe_filter` line at module scope.
- StrategyCoder is invoked by `src/agent/research/research-orchestrator.js` (likely via `_runStrategyCoder` or similar — verify at impl time). When the spec contains `inferred_universe_filter`, the orchestrator injects the predicate import into the prompt context.
- Lifecycle staging CLI (presumably `python3 -m strategies.lifecycle stage --strategy-id <X>` — verify) reads the new strategy file, extracts the `universe_filter` ref if present, and writes `manifest.metadata.universe_filter_ref` accordingly. Phase A's predicate-contract lint (`src/strategies/universe_lint.py`) runs at the same time so a broken predicate fails staging.
- 12 candidate predicates exist (Phase A: `sp500`, `large_cap`, `mid_cap`, `small_cap`, `etfs_only`, `options_eligible`, `high_vol_20d`, `tech_sector`, `value_quintile`, `momentum_quintile`, `low_corr`, `defensive`). PaperHunter must pick from this set or emit null. Free-form predicate emission is **out of scope** (matches Phase C's decision).
- `research_candidates.kind` defaults to `'paper'` (migration 025); orchestrator stages strategies derived from accepted candidates.

### 1.2 Decisions locked

| Question | Decision |
|---|---|
| Where the inferred predicate lives | One new optional top-level field `inferred_universe_filter` in `hunter_result_json` (JSONB). NO new DB column. Field is `null` when PaperHunter doesn't infer one. |
| PaperHunter prompt change | One new section between gate-evaluation and JSON-output: "Step 4b — Infer universe slice (optional)" with the 12-candidate list and a single sentence per candidate explaining when it fits. Output `null` if the paper is universe-agnostic. |
| Candidate set | Same 12 from `src/strategies/universe_default.py`. Phase D references them by name so any Phase A expansion of the set is automatically picked up. |
| StrategyCoder template change | When `inferred_universe_filter` is set, prompt injection adds a paragraph "Add this top-level statement after the imports: `from src.strategies.universe_default import <name> as universe_filter`". When null, prompt explicitly says "Do NOT define `universe_filter`; default `in_sp500` will apply." |
| Manifest registration | New lifecycle helper `lifecycle.register_strategy_predicate(strategy_id, predicate_name)` writes `manifest.metadata.universe_filter_ref`. Called by orchestrator at staging time. |
| Validation | Phase A's signature lint already enforces `(meta, as_of)` and the import ban. Phase D's only new validation: PaperHunter output's `inferred_universe_filter` must be `null` or a key in `CANDIDATE_PREDICATES` (whitelist enum). |
| Behavior on bad infer | Orchestrator drops the field (warns to log) and stages the strategy on the default. Never blocks staging on a bad predicate inference. |
| Fallback for legacy candidates | Strategies created **before** Phase D ships have no `inferred_universe_filter`; they default to `in_sp500` (Phase A behavior). Backfilling old candidates is out of scope (Phase C's Saturday cycle handles them). |
| Test posture | Unit tests for the predicate-extraction prompt (snapshot test on a curated set of 10 abstracts → expected predicate); integration test through a real PaperHunter dispatch on a fixture paper. |

---

## 2. Architecture

### 2.1 End-to-end flow

```
[Mon 09:00 ET — paper arrives in research_corpus from arxiv ingester]
   ↓
[Sat 10:00 ET — saturday-brain phase 3+4: corpus rate + PaperHunter fan-out]
   ↓ (per paper)
PaperHunter (claude-sonnet-4-6) extracts strategy_spec:
   - signal/formula/lookback/horizon (existing)
   - duplicate_fingerprint check (existing gate)
   - capability_gap check (existing gate)
   - inferred_universe_filter ← NEW Phase D step
       Returns one of: <12 candidate names> or null
   - hunter_result_json written with the new field
   ↓
[orchestrator promotes candidate → tier-A backtest → if pass, stages strategy]
   ↓
StrategyCoder (claude-sonnet-4-6) emits Python file:
   - if spec.inferred_universe_filter:
       inserts `from src.strategies.universe_default import {name} as universe_filter`
     else:
       no universe_filter at module scope → defaults to sp500
   - existing class contract unchanged
   ↓
lifecycle.stage_strategy(file_path):
   - existing: copy file to implementations/, register in manifest
   - NEW: if `universe_filter` import detected in file,
         write manifest.metadata.universe_filter_ref accordingly
   - existing: run universe_lint.py against the file (Phase A gate)
   - existing: run sandbox-check at transition (Phase A)
   ↓
[Strategy is live with explicit predicate; UniverseResolver picks it up next cycle]
```

### 2.2 PaperHunter prompt extension

`src/agent/prompts/subagents/paperhunter.md` — new section after current Step 4 (gates), before the JSON output section:

```markdown
## Step 5 — Infer universe slice (optional)

Based on the paper's strategy, pick ONE of the 12 candidate predicates that
best matches its intended universe, OR return null if the paper is universe-
agnostic. Output this in the JSON as `inferred_universe_filter`.

### Candidate slices (paste the predicate NAME, lowercase):

| Name | When it fits |
|---|---|
| `sp500` | Generic large-cap US equity strategies; mean-reversion on liquid names; momentum factor work. The default. |
| `large_cap` | Strategies that need fundamental data quality and don't tolerate small-cap noise. |
| `mid_cap` | Mid-cap-specific anomalies (e.g., size-effect work in $2B–$10B range). |
| `small_cap` | Small-cap value, micro-cap momentum, low-priced effects. |
| `etfs_only` | Pair trades on sector ETFs; rotation strategies; benchmark-anchored work. |
| `options_eligible` | Anything involving options pricing, IV, skew, gamma, pin risk. |
| `high_vol_20d` | Volatility-arbitrage / mean-reversion on high-realized-vol names. |
| `tech_sector` | Tech-specific factor work; semiconductor cycles; software momentum. |
| `value_quintile` | Value factor (P/E, P/B, EV/EBITDA tilts). |
| `momentum_quintile` | Cross-sectional momentum factor strategies. |
| `low_corr` | Long/short market-neutral strategies that need low pairwise correlation. |
| `defensive` | Low-volatility / quality tilt; staples + utilities + healthcare emphasis. |

### Rules

- Pick ONE name from the table or `null`. No free-form values; no
  combinations. The orchestrator will reject invalid names and fall back
  to default (`sp500`).
- If unsure, return `null` — the operator's Saturday `universe-recs` cycle
  will re-evaluate later.
- Bias toward `null` for cross-asset or non-equity strategies.

### Examples (read these patterns, don't memorize):

- Paper on "pin risk in SPY weekly options around earnings" → `options_eligible`
- Paper on "post-earnings drift in technology stocks 2010-2022" → `tech_sector`
- Paper on "macro regime conditioning of long-short factor portfolios" → `null`
  (universe is implicit in the factor construction; let `sp500` apply)
- Paper on "low-vol anomaly in small-caps" → `small_cap` (the universe IS the
  anomaly's perimeter)
```

JSON output section then mentions the new field:
```markdown
### JSON output schema

```json
{
  "candidate_id": "{{CANDIDATE_ID}}",
  "source_url": "{{SOURCE_URL}}",
  "rejection_reason_if_any": null,
  "strategy_spec": {
    ...
  },
  "inferred_universe_filter": "<name|null>"   ← NEW Phase D field
}
```
```

### 2.3 StrategyCoder prompt extension

`src/agent/prompts/subagents/strategycoder.md` — add a new section after "Required Artifacts → Artifact 1 — Implementation file":

```markdown
### Universe predicate

The orchestrator injects the inferred predicate into your context as:
- `INFERRED_UNIVERSE_FILTER = <name>` (one of the 12 candidates) — OR
- `INFERRED_UNIVERSE_FILTER = null`

**If `INFERRED_UNIVERSE_FILTER` is a name:**
Add this line directly after your standard imports (before the strategy class
definition):

```python
from src.strategies.universe_default import {INFERRED_UNIVERSE_FILTER} as universe_filter
```

The `universe_filter` symbol must be at module scope so Phase A's lint
(`src/strategies/universe_lint.py`) finds it.

**If `INFERRED_UNIVERSE_FILTER` is null:**
Do NOT define `universe_filter`. The strategy will inherit the default
(`in_sp500`) — no code needed.

**Never inline a custom predicate body.** Phase D restricts strategy files
to importing one of the 12 pre-vetted predicates (or omitting the symbol).
Custom predicate emission is out of scope; if you believe none of the 12
fit, leave `universe_filter` undefined and the operator will adopt a
better one via the Saturday `universe-recs` cycle.
```

### 2.4 Orchestrator wiring

`src/agent/research/research-orchestrator.js` — the staging branch that invokes StrategyCoder:

```js
// When invoking StrategyCoder, inject the predicate context
const inferredFilter = candidate.hunter_result_json?.inferred_universe_filter ?? null;
const validFilter = await _validateInferredFilter(inferredFilter);  // null if invalid
const promptCtx = {
  ...existingCtx,
  INFERRED_UNIVERSE_FILTER: validFilter,
};
const codeResult = await _runStrategyCoder(candidate, promptCtx);

async function _validateInferredFilter(name) {
  if (name == null) return null;
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.universe_default import CANDIDATE_PREDICATES; '
    + 'import sys; sys.exit(0 if sys.argv[1] in CANDIDATE_PREDICATES else 1)',
    name], {encoding:'utf8'});
  if (r.status !== 0) {
    console.warn(`[orchestrator] PaperHunter emitted invalid predicate '${name}', falling back to default`);
    return null;
  }
  return name;
}
```

After StrategyCoder returns, orchestrator stages via:

```js
const lifecycleRes = spawnSync(PYTHON, [
  '-m','src.strategies.lifecycle',
  'stage',
  '--strategy-id', candidate.candidate_id,
  '--file', codeResult.filePath,
  // NEW Phase D: if the file declares universe_filter, lifecycle picks it up
],{encoding:'utf8'});
```

### 2.5 Lifecycle helper

`src/strategies/lifecycle.py` — small addition (does not affect existing fields):

```python
def register_strategy_predicate(self, strategy_id: str, predicate_name: str | None) -> None:
    """Write metadata.universe_filter_ref. Called by stage() when the
    new strategy file's module-scope `universe_filter` import is detected.
    Idempotent. Validated against CANDIDATE_PREDICATES whitelist.
    """
    from src.strategies.universe_default import CANDIDATE_PREDICATES
    if predicate_name is not None and predicate_name not in CANDIDATE_PREDICATES:
        raise ValueError(f"predicate '{predicate_name}' not in candidate set")
    manifest = json.loads(MANIFEST.read_text())
    entry = manifest['strategies'].get(strategy_id)
    if not entry: raise ValueError(f"strategy {strategy_id} not staged")
    if predicate_name:
        entry.setdefault('metadata', {})['universe_filter_ref'] = predicate_name
    else:
        entry.get('metadata', {}).pop('universe_filter_ref', None)
    tmp = MANIFEST.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=2))
    os.fsync(open(tmp).fileno())
    os.rename(tmp, MANIFEST)
```

`lifecycle.stage(strategy_id, file_path)` calls a small predicate-detector:

```python
def _detect_module_predicate(file_path: Path) -> str | None:
    """Parse the new strategy file for a module-scope import:
    `from src.strategies.universe_default import <name> as universe_filter`
    Returns <name> or None.
    """
    import ast
    tree = ast.parse(file_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'src.strategies.universe_default':
            for alias in node.names:
                if alias.asname == 'universe_filter':
                    return alias.name
    return None
```

`stage()` then:
```python
predicate = _detect_module_predicate(file_path)
self.register_strategy_predicate(strategy_id, predicate)
```

This is the ONLY new code touching `lifecycle.py`. **Critical:** no new top-level `StrategyRecord` fields (per [[feedback-lifecycle-silent-strip]]) — `universe_filter_ref` was already added by Phase A.

### 2.6 Doctor / system_checks

Phase D is small enough that no new doctor checks are warranted. The existing Phase A check `_check_union_universe_size` covers the risk that PaperHunter starts emitting predicates that cause the union to spike or collapse.

A single new system_check is added as a soft signal:

```
src/system_checks/checks/papermint_predicate_coverage.py
  @check(name='papermint_predicate_coverage', tags=['agents','strategies'], requires=['db'])
  Returns PASS if ≥ 50% of last 30d's new candidates emitted a non-null
  inferred_universe_filter; WARN if 20-50%; FAIL only if 0% (suggests
  prompt regression).
```

### 2.7 Dashboard

Minor extension to operator dashboard Strategy Health row:

```
src/channels/dashboard/server.js (:7870)
  + GET /api/papermint-recent
    Last 30d candidates from research_candidates with their inferred predicate
    + actual adopted predicate (post-stage) + adoption_lag_days.
    UI tile: small table showing recent net-new strategies' starting predicate.
```

---

## 3. Components

### 3.1 New files

```
src/system_checks/checks/papermint_predicate_coverage.py    ~60 LoC

tests/test_paperhunter_predicate_inference.py               ~150 LoC
tests/test_strategycoder_predicate_emission.py              ~120 LoC
tests/test_lifecycle_predicate_detection.py                 ~100 LoC
tests/test_phase_d_smoke.py                                 ~80 LoC

docs/sp2-papermint-runbook.md                               ~80 LoC
  (Operator runbook: how to validate PaperHunter's emissions, when to
   override via Phase C re-evaluation.)
```

### 3.2 Modified files

```
src/agent/prompts/subagents/paperhunter.md
  + New section §5 "Infer universe slice (optional)" + JSON schema field.

src/agent/prompts/subagents/strategycoder.md
  + New section "Universe predicate" describing the optional import line.

src/agent/research/research-orchestrator.js
  + _validateInferredFilter(name) helper.
  + Inject INFERRED_UNIVERSE_FILTER into StrategyCoder prompt context.

src/strategies/lifecycle.py
  + register_strategy_predicate(strategy_id, predicate_name)
  + _detect_module_predicate(file_path)
  + stage() invokes both.

src/system_checks/checks/__init__.py
  + import papermint_predicate_coverage

src/channels/dashboard/server.js
  + GET /api/papermint-recent
src/channels/dashboard/public/index.html
  + UI panel "Recent net-new strategies (predicate at mint)"
```

### 3.3 `.env` changes

```
ADD:
  OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1   (default ON post-deploy; gate)

REMOVE: (none)
```

The gate is conservative: when 0, PaperHunter prompts revert to the pre-Phase-D version (no §5), StrategyCoder ignores the predicate context, lifecycle skips `_detect_module_predicate`. Allows fast rollback.

### 3.4 Schema

No new tables. No new columns. `research_candidates.hunter_result_json` is JSONB; the new `inferred_universe_filter` field lives inside that blob.

### 3.5 Memory + docs updates

```
/root/.claude/projects/-root/memory/project_sp2_phase_d_research_hooks.md  (NEW)
  PaperHunter §5 + StrategyCoder predicate-injection + lifecycle helper.
  Whitelist-only emission (12 candidates); never free-form.

MEMORY.md  index update.

CLAUDE.md  Recent Changes entry post-deploy.

ARCHITECTURE.md  Per-Strategy Universe Resolution section extended with
  "Predicate-at-mint (Phase D)" subsection.
```

---

## 4. Data Flow

### 4.1 New-strategy minting flow (post Phase D)

```
[Sat 10:00 ET — saturday-brain phases 1-2]
   arxiv/openalex/etc → research_corpus
   Mastermind corpus rates → research_candidates inserted (implementable=true)

[Sat 10:30 ET — saturday-brain phases 3-4]
   PaperHunter fan-out (paperhunter.js LangGraph):
     For each candidate:
       claude-sonnet-4-6 extracts strategy_spec + decides inferred_universe_filter
       hunter_result_json upserted with the new field
       Gate failures still produce {rejection_reason_if_any: "..."} as before

[Sat 11:00 ET — saturday-brain phase 5-6]
   data_tier_filter narrows accepted candidates
   Tier-A backtest runs (uses default sp500 predicate at this stage —
     intentional: backtest qualification is universe-agnostic for tier-A)

[Tue 12:00 ET — operator approves promotion]
   orchestrator.stage_candidate(candidate_id):
     - reads research_candidates.hunter_result_json.inferred_universe_filter
     - validates against CANDIDATE_PREDICATES whitelist
     - injects INFERRED_UNIVERSE_FILTER into StrategyCoder context
     - StrategyCoder emits file with universe_filter import (or without)
     - lifecycle.stage(strategy_id, file_path):
         _detect_module_predicate → registers manifest.metadata.universe_filter_ref
     - universe_lint.py runs (Phase A)
     - sandbox check on transition (Phase A)
   Strategy enters CANDIDATE state with explicit predicate already set.

[Mon 10:00 ET — daily cycle dispatches the new strategy]
   UniverseResolver reads manifest.metadata.universe_filter_ref → applies
   the candidate predicate. Strategy's signals fire on its intended slice.
```

### 4.2 Soft rollback flow

```
[Operator notices PaperHunter making bad picks]
   OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0
   systemctl restart johnbot.service (or saturday-brain timer dies and restarts)
   Effect: new candidates from next Saturday onward get no inferred predicate
   Existing strategies with adopted predicates unaffected
   Wall: ≤30s.
```

---

## 5. Phase Ordering Within Phase D

Phase D is the smallest of the four — a single deploy pass works:

```
1. PaperHunter prompt extension                                          (Task 1)
2. StrategyCoder prompt extension                                        (Task 2)
3. Orchestrator validation + prompt injection                            (Task 3)
4. Lifecycle helper + predicate detection                                (Task 4)
5. System_check + dashboard tile                                         (Task 5)
6. Smoke + docs + memory                                                 (Task 6)
7. PR + supervised first PaperHunter run                                 (Task 7)
```

---

## 6. Error Handling + Rollback

### 6.1 Failure-mode matrix

| Failure | Detection | Response | Severity |
|---|---|---|---|
| PaperHunter emits invalid predicate name | orchestrator validation | Drop the field; log; stage with default. | LOW |
| PaperHunter omits the field entirely (legacy prompt) | orchestrator treats missing as null | Stage with default. Backward compat. | LOW |
| StrategyCoder ignores the injected context | static check at lifecycle stage time | Predicate not registered; strategy on default; alert to log. | LOW |
| StrategyCoder writes a malformed `universe_filter` import | `_detect_module_predicate` returns None; universe_lint.py fails (Phase A) | Staging blocked at lint; operator fixes file or re-invokes coder. | MEDIUM |
| New strategy's predicate returns empty universe at first cycle | Phase A `_check_union_universe_size` warns | Strategy gets 0 signals; alert; operator overrides to default via lifecycle CLI. | MEDIUM |
| PaperHunter starts emitting same predicate for everything (prompt regression) | papermint_predicate_coverage system_check | Soft-fail signal; operator investigates prompt drift; can roll back. | LOW |
| Phase A lint catches a new module-scope import that wasn't anticipated | Phase A CI gate | PR can't merge until lint passes; orchestrator-side validation should catch this earlier. | LOW |

### 6.2 Rollback ladder

```
LEVEL 1 — Disable predicate-at-mint
  OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0
  Restart johnbot.service
  Effect: next saturday-brain run, PaperHunter behaves as pre-Phase-D
          (no §5 section in prompt). StrategyCoder ignores predicate context.
          Lifecycle skips _detect_module_predicate.
  Existing minted strategies KEEP their explicit predicates.
  Wall: ≤30s.

LEVEL 2 — Revert a specific new strategy to default
  python3 -m src.strategies.lifecycle_universe_adoption revert --strategy-id <X>
  (Reuses Phase C's helper.)
  Wall: ≤5s.

LEVEL 3 — Full Phase D revert
  git revert <Phase-D-merge-SHA>
  No migrations to roll back.
  Existing minted strategies KEEP their explicit predicates (their files
    still declare `universe_filter`; lifecycle won't strip them, only Level 2
    will).
  Wall: ~15min.
```

### 6.3 Pre-deploy operator checklist

```
[ ] Phase C in production ≥ 2 weeks
[ ] At least 5 strategies on explicit (non-default) predicates via Phase C
[ ] Prompt diff reviewed (paperhunter.md + strategycoder.md)
[ ] OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1 in production .env
[ ] First saturday-brain run after deploy: operator inspects 10 random
    hunter_result_json blobs and confirms inferred_universe_filter values look sane
[ ] If sanity-check fails → Level 1 rollback within 24h
```

---

## 7. Testing + Validation

### 7.1 Unit tests

```
tests/test_paperhunter_predicate_inference.py
  - Snapshot test: 10 curated abstract texts → expected predicate
    (one per CANDIDATE_PREDICATES value + 1 null case).
    Uses fixture abstracts; runs claude-bin --print against the prompt;
    asserts output JSON's inferred_universe_filter is in the expected set.
    Marked @pytest.mark.opus (costly — operator runs manually pre-deploy).

  - Static check: paperhunter.md contains the new §5 section and the
    inferred_universe_filter field in the JSON schema.

tests/test_strategycoder_predicate_emission.py
  - Static check: strategycoder.md contains the new universe-predicate section.
  - Mock invocation: given a spec with inferred_universe_filter='large_cap',
    assert the emitted file contains
      `from src.strategies.universe_default import large_cap as universe_filter`.

tests/test_lifecycle_predicate_detection.py
  - _detect_module_predicate returns 'large_cap' for a file with the import
  - returns None for a file without
  - returns None for a file with the import but no `as universe_filter` alias
  - register_strategy_predicate writes manifest correctly
  - register_strategy_predicate rejects unknown predicate names
  - register_strategy_predicate(None) deletes any existing ref
```

### 7.2 Integration: tests/test_phase_d_smoke.py

```
1. Static: both prompts contain the new sections
2. Lifecycle: create a fixture strategy file, call stage(), assert
   manifest.metadata.universe_filter_ref matches the import
3. Orchestrator validator: call _validateInferredFilter with a name +
   with garbage; assert correct null fallback
4. End-to-end mock: hunter_result_json with inferred_universe_filter=
   'options_eligible' → orchestrator stages → manifest reflects it
```

### 7.3 Pre-deploy soak

Phase D doesn't have a multi-week soak the way A/B/C do; it has a single
saturday-brain cycle to confirm:

- First Saturday post-deploy: operator runs `saturday-brain` with `--dry-run` first.
- Then live; operator manually inspects every `inferred_universe_filter` value in `hunter_result_json` written that cycle.
- If >= 80% look reasonable, allow normal operation; otherwise Level 1 rollback.

### 7.4 Out of scope for Phase D

- Free-form predicate emission (forever out of scope per Phase C's decision).
- Retroactive predicate inference on past `research_candidates` (Phase C's Saturday cycle handles that).
- PaperHunter inferring multiple predicates per paper (single predicate per strategy is the model).
- Cross-paper predicate sharing (e.g., "this set of 5 papers all suggest tech_sector"). Each paper handled independently.
- StrategyCoder generating *new* candidate predicates outside the 12 (rejected per Phase C decision).

---

## 8. References

- Parent spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.4
- Phase A: `src/strategies/universe_default.py` (CANDIDATE_PREDICATES), `src/strategies/universe_lint.py`, lifecycle `universe_filter_ref` field
- Phase C: `src/strategies/lifecycle_universe_adoption.py` (revert helper reused), Discord adoption flow context
- PaperHunter: `src/agent/prompts/subagents/paperhunter.md`, `src/agent/graphs/paperhunter.js`, `src/agent/research/research-orchestrator.js`
- StrategyCoder: `src/agent/prompts/subagents/strategycoder.md`
- Lifecycle: `src/strategies/lifecycle.py` (`stage`, `StrategyRecord` — DO NOT add top-level fields per [[feedback-lifecycle-silent-strip]])
- Schema: `research_candidates.hunter_result_json` (JSONB, migration 025) — accommodates new field with no schema change
- Memory: `feedback_universe_predicate_contract.md` (the contract Phase D defers to)
