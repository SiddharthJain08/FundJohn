# SP-2 Phase D — Predicate-at-mint Runbook

## What changed

SP-2 Phase D adds universe-predicate awareness to the research-to-strategy pipeline:

1. **PaperHunter** (`src/agent/prompts/subagents/paperhunter.md`, §5 "Infer universe slice") — for each paper it evaluates, the agent infers which ONE of the 12 `CANDIDATE_PREDICATES` best matches the paper's thesis (or writes `null` if none applies). The choice is written as `inferred_universe_filter` into `research_candidates.hunter_result_json` (JSONB — no schema migration required).

2. **StrategyCoder** (`src/agent/prompts/subagents/strategycoder.md`, §"Universe predicate") — when `INFERRED_UNIVERSE_FILTER` is present in its context, the coder emits a module-scope import at the top of the generated strategy file:
   ```python
   from src.strategies.universe_default import <name> as universe_filter
   ```
   If no filter was inferred, the import is omitted and the strategy inherits the default `sp500`.

3. **Orchestrator** (`src/agent/research/research-orchestrator.js`) — validates `inferred_universe_filter` against the Python whitelist via `_validateInferredFilter`; unknown names fall back to `null`. The validated name is threaded into the StrategyCoder context and written into the queued `strategy_spec`. Gated by `OPENCLAW_PHASE_D_PREDICATE_AT_MINT`.

4. **Lifecycle** (`src/strategies/lifecycle.py`, `_detect_module_predicate`) — at `register()` time, AST-parses the strategy's implementation file for a top-level `from src.strategies.universe_default import <name> as universe_filter` import and writes `metadata.universe_filter_ref = "src.strategies.universe_default:<name>"` to the manifest via `save_manifest`. This is the resolver-loadable `module:attr` form used by `UniverseResolver`.

### No schema migration

The new `inferred_universe_filter` field rides `research_candidates.hunter_result_json` (JSONB). No migration is needed.

### New system_check + dashboard tile

- `papermint_predicate_coverage` check (agents + strategies tag) — queries the 30-day window for candidates with a non-null `inferred_universe_filter` and reports coverage %.
- Operator dashboard `:7870` tile "Recent net-new strategies (predicate at mint)".

---

## How to validate this Saturday's batch

After saturday-brain completes (Sat ~10 AM ET corpus run + strategy mint):

**Step 1 — Query the candidates:**

```sql
SELECT candidate_id,
       hunter_result_json->>'inferred_universe_filter' AS pred,
       left(hunter_result_json->>'strategy_thesis', 80) AS thesis
FROM research_candidates
WHERE submitted_at > NOW() - INTERVAL '24 hours';
```

**Step 2 — Eyeball (pred, thesis) pairs:**

- ≥ 80% pairs look reasonable (e.g. momentum thesis → `sp500`, small-cap thesis → `small_cap_liquid`) → leave gate on.
- < 50% reasonable OR any obviously absurd match (e.g. crypto thesis → `tech_sector`, options vol thesis → `no_otc`) → trigger Level 1 rollback immediately.

**Step 3 — Check lifecycle registration:**

For any strategy that just minted, confirm the manifest field was written:

```bash
python3 -c "
import json, sys
m = json.load(open('src/strategies/manifest.json'))
for sid, s in m['strategies'].items():
    ref = s.get('metadata', {}).get('universe_filter_ref')
    if ref: print(sid, ref)
"
```

Also verify via SQL:

```sql
SELECT strategy_id, metadata->>'universe_filter_ref' AS ref
FROM strategies
WHERE created_at > NOW() - INTERVAL '24 hours';
```

---

## Rollback ladder

### Level 1 — Soft disable (gate off, new candidates get null)

```bash
# On VPS:
# Edit /root/openclaw/.env and set:
OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0
# Then restart:
sudo systemctl restart johnbot.service
```

Already-minted strategies keep their explicit import. No DB writes needed.

### Level 2 — Per-strategy revert

Strips the `universe_filter_ref` from a specific strategy (reverts to `sp500` default):

```bash
python3 -m src.strategies.lifecycle_universe_adoption revert --strategy-id <STRATEGY_ID>
```

This reads the most recent `universe_filter_adopted` row in `lifecycle_audit_log`, restores `before_state`, appends a `universe_filter_reverted` row, and renames the manifest atomically. Audit rows are never deleted.

### Level 3 — Full git revert

```bash
git log --oneline | grep "sp2-d"   # find the Phase D merge SHA
git revert <SHA>
# Then redeploy:
sudo systemctl restart johnbot.service
```

Existing minted strategies keep their explicit predicate import in their `.py` files; apply Level 2 per-strategy to strip those if desired.

---

## When to re-enable after rollback

1. Identify the root cause: prompt phrasing, whitelist gap, or orchestrator bug.
2. Fix and run the smoke suite: `python3 -m pytest tests/test_phase_d_smoke.py -v`
3. Re-enable requires at least one operator-supervised saturday-brain run before leaving unattended:
   ```bash
   OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1
   sudo systemctl restart johnbot.service
   ```
4. Monitor `#research-candidates` Discord channel during the next saturday-brain run.

---

## Day-1 coverage warning

> **The `papermint_predicate_coverage` system_check will read 0% (FAIL when gate ON) on day 1 after deploy.**

The check queries a 30-day window. All candidates in that window were submitted before Phase D shipped and therefore have `inferred_universe_filter = null`. Do NOT alarm on a 0% reading in the first days after deploy. Judge coverage only on candidates submitted AFTER the deploy timestamp. The check will self-correct as new saturday-brain batches accumulate.

---

## Gate summary

| Env var | Default (code) | Intended live state | Effect when 0 / absent |
|---|---|---|---|
| `OPENCLAW_PHASE_D_PREDICATE_AT_MINT` | absent = OFF | `=1` (set in `.env`) | Orchestrator skips validation; `inferred_universe_filter` never injected into coder ctx |

Note: the code treats an ABSENT variable as OFF (false). The live VPS `.env` must contain `OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1` to activate Phase D.

---

## Related files

- Spec: `docs/superpowers/specs/2026-05-22-sp2-phase-d-research-hooks-design.md`
- Plan: `docs/superpowers/plans/2026-05-22-sp2-phase-d-research-hooks.md`
- Smoke tests: `tests/test_phase_d_smoke.py`
- Orchestrator: `src/agent/research/research-orchestrator.js` (`_validateInferredFilter`)
- Lifecycle: `src/strategies/lifecycle.py` (`_detect_module_predicate`)
- Predicates: `src/strategies/universe_default.py` (`CANDIDATE_PREDICATES`)
