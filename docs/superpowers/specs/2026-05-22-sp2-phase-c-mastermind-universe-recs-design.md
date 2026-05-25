# SP-2 Phase C: Mastermind Universe-Recs — Design

**Spec date:** 2026-05-22
**Author:** BotJohn (extension of SP-2 umbrella)
**Status:** Pending operator review (cannot start before Phase B Soak B clears + ≥ 5y `ticker_metadata_snapshots` depth confirmed)
**Parent:** `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.3
**Predecessors:** Phase A (PR #8), Phase B (`feat/sp2-phase-b-5y-backfill`)
**Branch:** `feat/sp2-phase-c-mastermind-universe-recs`

---

## 1. Context

Phase A gave every strategy a default predicate (`in_sp500`). Phase B gave Mastermind the historical data needed to evaluate alternative predicates point-in-time. Phase C closes the loop: every Saturday, MastermindJohn (Opus 4.7 1M) re-evaluates each live strategy against the 12 candidate predicates and proposes a switch when one materially out-performs the current choice (default or previously adopted). Operator approves via Discord reaction; adopting writes the predicate ref into the strategy manifest and the strategy file picks it up on its next lifecycle read.

The bet: Opus, given a strategy's code + thesis + 12 backtest results across the same fixed time window, can pick the slice that best matches the strategy's *intent* rather than its current accidental scope.

### 1.1 Anchored facts (2026-05-22)

- `src/agent/curators/run_mastermind.js` is the dispatcher; modes today: `saturday-brain`, `corpus`, `comprehensive-review`, `position-recs`, `paper-expansion`. Phase C adds `--mode universe-recs`.
- Saturday timer pile-up (current — verified `ls docs/*.timer`):
  - 10:00 ET `openclaw-mastermind-corpus.timer` → `saturday-brain` (subsumes corpus + paper-expansion)
  - 18:00 ET `openclaw-strategy-review.timer` → `comprehensive-review`
  - 19:00 ET `openclaw-position-recs.timer` → `position-recs`
  - Phase C adds: **20:00 ET** `openclaw-universe-recs.timer` → `universe-recs`. Slot is empty; budget impact analyzed below.
- `_opus_oneshot.js` handles single-turn Opus prompts via stdin (avoids ARG_MAX). All Phase C calls go through this helper.
- `comprehensive_review.js` is the closest existing curator to copy from — per-strategy iteration with `_query`, `_buildTradePack`, prompt assembly, `runOneShot`, JSON parsing, DB write, Discord post.
- Migration 112 (`strategy_universe_recommendations`) was landed by Phase A — Phase C is the first writer.
- 12 candidate predicates live in `src/strategies/universe_default.py` (Phase A).
- `src/strategies/lifecycle.py` already supports `metadata.universe_filter_ref` (Phase A). Phase C adds an `adopt_universe_recommendation(rec_id)` helper that does the manifest edit + lifecycle write atomically.
- TradeJohn handoff (`trade_handoff_builder.py`) reads per-strategy state; Phase C's predicate change is invisible to it (resolver-mediated).
- Opus 4.7 cost on 1M context: ~$15/$75 per M input/output tokens. A `universe-recs` per-strategy call: ~50KB code + ~5KB thesis + ~3KB backtest grid (~58KB → ~14k input tokens) + ~2KB JSON output (~500 output tokens) ≈ $0.25 per strategy. 51 live strategies × $0.25 = $12.75 / week. Budget cap per spec: $8/strategy hard ceiling; weekly cap: $400 (matches Saturday-brain's existing cap).

### 1.2 Decisions locked

| Question | Decision |
|---|---|
| Candidate set | Finite, pre-vetted: the 12 predicates from `src/strategies/universe_default.py` (Phase A). Opus picks ONE of them or "no change". Free-form predicate emission is **out of scope for SP-2** (would require its own lint+sandbox+approval surface). |
| Backtest harness | `regime_blended_backtest.py` (the production sizer's backtest) with `resolver` argument substituted per candidate. Phase A made this the per-bar `as_of` resolver path; Phase C just feeds it a `MockResolver` that returns the candidate predicate's universe at each `as_of`. |
| Backtest window | Trailing 12 months ending Saturday 18:00 ET. Frozen per run (so re-runs of the same Saturday produce deterministic grids). |
| Metric set per candidate | `{sharpe, max_dd_pct, win_rate, mean_universe_size, trades_n, sortino, calmar, mean_holding_days}` (8 metrics). Determinism: same window + seed → identical grid (regression-tested). |
| Opus prompt structure | (1) Strategy source code; (2) thesis from manifest description; (3) 12 × 8 grid; (4) current predicate; (5) operator-preference block (Sharpe ≥ 0.5, MaxDD ≤ 20%, prefer simpler predicates ceteris paribus); (6) hard JSON-output schema. |
| Output schema | `{choice: <candidate_name or "no_change">, rationale: <≤500 chars>, confidence: 0.0-1.0, expected_uplift_sharpe: float, risks: [str]}` |
| Discord flow | Post 1 message per recommendation to `#universe-recs` with 3 reactions: ✅ approve, ❌ reject, ⏸ defer. Operator clicks; bot listener (`src/channels/discord/bot.js`) handles the lifecycle write. |
| Lifecycle adoption | `lifecycle.adopt_universe_recommendation(rec_id)` does an atomic transaction: update `strategy_universe_recommendations.adopted=true`, edit manifest.json `metadata.universe_filter_ref`, emit `audit_log` row. Strategy file does NOT need editing — resolver reads `universe_filter_ref` from manifest. |
| Cost guardrails | Per-strategy budget cap $8 (matches comprehensive-review pattern); per-run weekly cap $400; circuit-breaker on cumulative cost. |
| Determinism guarantee | Backtest grid serialization includes input hash (universe+window+strategy_id+candidate_set_version); re-running same Saturday → identical grid → identical Opus input → identical decision (modulo Opus sampling variance, controlled by `temperature=0`). |
| Reversibility | Adoptions are reversible: operator runs `python3 -m strategies.lifecycle revert_universe_recommendation --strategy-id <X>` which restores the prior `universe_filter_ref` (audit log preserves history). |
| First-run posture | The first Saturday run is **operator-supervised**: operator reviews all rationales before clicking any approve. Subsequent runs operate normally. |

---

## 2. Architecture

### 2.1 End-to-end flow

```
Sat 20:00 ET   openclaw-universe-recs.timer fires
               → src/agent/curators/run_mastermind.js --mode universe-recs

For each strategy in live_strategies:
  1. Load:
     - source code (from src/strategies/implementations/<file>.py)
     - thesis (from manifest.description)
     - last 12mo closed trades (from execution_signals JOIN signal_pnl)
     - current universe_filter_ref (from manifest.metadata)

  2. Build backtest grid (12 candidates × 8 metrics):
     For each candidate in CANDIDATES:
       MockResolver = lambda strategy_id, as_of: candidate(meta_at(as_of))
       result = regime_blended_backtest.run(
         strategy=strategy,
         start=today - 365,
         end=today,
         resolver=MockResolver,
         seed=42,
       )
       grid[candidate_name] = {sharpe, max_dd, win_rate, mean_uni_size,
                                trades_n, sortino, calmar, mean_holding}

  3. Pack into Opus prompt (template at
     src/agent/prompts/subagents/universe-recommender.md):
       - Strategy code
       - Thesis
       - Grid (markdown table)
       - Current predicate name
       - Operator preferences
       - Required JSON output schema

  4. Opus → _opus_oneshot.runOneShot({prompt, model: 'claude-opus-4-7[1m]',
                                       timeoutMs: 30 * 60 * 1000})
     → returns text + cost

  5. Parse JSON output; validate against schema; reject malformed.

  6. INSERT INTO strategy_universe_recommendations
       (strategy_id, recommended_at, current_predicate, candidate_predicate,
        candidate_set_id, backtest_summary, rationale, approved=NULL,
        mastermind_cost_usd)

  7. Post to #universe-recs Discord:
     ```
     [universe-recs 2026-XX-XX] S5_max_pain (id=42)
     Current: sp500 (sharpe 1.10, dd 14%, trades 87)
     Suggested: large_cap_options (sharpe 1.42, dd 11%, trades 73)
     Confidence: 0.82  Expected uplift Δsharpe: +0.32
     Rationale: pin-risk strategy needs liquid weeklies; large_cap_options
       restricts to the top 200 by ADV with weekly chains, dropping the
       low-volume small-caps that drag win_rate.
     Risks: ["lower trade frequency may slow PnL realization"]
     Grid: <link to dashboard /universe-recs/42>
     React: ✅ approve | ❌ reject | ⏸ defer
     ```

Operator reaction handler in src/channels/discord/bot.js:
  ✅ → POST to dashboard /api/universe-recs/<rec_id>/approve → invokes
       lifecycle.adopt_universe_recommendation(rec_id)
       → updates strategy_universe_recommendations.{approved, adopted}=true
       → edits manifest.json metadata.universe_filter_ref
       → writes lifecycle audit_log
       → reacts ✔ to the source message
  ❌ → marks approved=false, adopted=false; reacts ✔
  ⏸ → leaves approved=NULL (will re-emerge next Saturday with refreshed grid)
```

### 2.2 The Mastermind prompt structure

`src/agent/prompts/subagents/universe-recommender.md` (the prompt template, similar to existing subagent prompts in `src/agent/prompts/subagents/`):

```markdown
# Universe Recommender (Phase C)

You are MastermindJohn, the Opus 4.7 1M-context strategy curator. Your job
this turn: pick the universe predicate that best matches a single strategy's
intent and expected behavior.

## Inputs

### Strategy: {{strategy_id}} — {{strategy_name}}

**Thesis** (from manifest description):
{{thesis}}

**Source code** (~{{loc}} lines):
```python
{{source_code}}
```

**Current predicate:** `{{current_predicate}}`

### Candidate-predicate backtest grid (last 12 months)

Same time window. Same seed (42). Same regime_blended backtest engine.
Only difference: which tickers the resolver returned per bar.

| Candidate | Sharpe | MaxDD% | WinRate | MeanUniSize | Trades | Sortino | Calmar | MeanHoldDays |
|---|---|---|---|---|---|---|---|---|
{{#each grid}}
| {{name}} | {{sharpe}} | {{max_dd_pct}} | {{win_rate}} | {{mean_universe_size}} | {{trades_n}} | {{sortino}} | {{calmar}} | {{mean_holding_days}} |
{{/each}}

### Operator preferences

- Prefer Sharpe ≥ 0.5; FAIL if no candidate clears 0.3.
- Prefer MaxDD ≤ 20%.
- All else equal, prefer SIMPLER predicates (sp500 < large_cap < large_cap_options < ...).
- A 0.05 Sharpe improvement is NOT material; demand at least +0.15 to switch
  off the current predicate.

## Required output

Reply with EXACTLY one fenced JSON block, no prose:

```json
{
  "choice": "<one of: {{candidate_names}} OR 'no_change'>",
  "rationale": "<= 500 chars explaining why this slice fits the strategy's intent>",
  "confidence": <0.0 - 1.0>,
  "expected_uplift_sharpe": <float; 0 if no_change>,
  "risks": ["<short risk phrase>", ...]
}
```

If you cannot defend a switch under the operator preferences, choose
`"no_change"` and explain why. Do not invent metrics not present in the grid.
```

### 2.3 Determinism + reproducibility

To make Opus's output trustworthy under operator review:

- Grid generation is **fully deterministic**: same `(strategy_id, week_ending_date, candidate_set_version)` → identical grid bytes. SHA-256 of grid JSON is stored alongside the recommendation in `backtest_summary.grid_sha256`.
- Opus call uses `temperature=0` and the same prompt template. Recommendation rows store `model_id` + `prompt_template_sha256` for audit.
- Re-running the same Saturday's recommender (operator-invoked `--strategy-id X --week 2026-09-13`) MUST produce a recommendation with identical grid SHA but the rationale may vary (Opus sampling is not bit-deterministic at temperature=0 across model versions). Operator-side test: `grid_sha256` is the structural ground truth.

### 2.4 Backtest harness — `MockResolver`

The standard `UniverseResolver` reads from `ticker_metadata_snapshots` and applies the predicate registered for the strategy. For Phase C grid generation we need to apply a *different* predicate without modifying the manifest. `MockResolver`:

```python
# src/strategies/universe_resolver.py — add helper
class MockResolver(UniverseResolver):
    def __init__(self, db_conn, prices_parquet, options_parquet, predicate):
        super().__init__(db_conn, prices_parquet, options_parquet)
        self._predicate = predicate
    def resolve(self, strategy_id: str, as_of: date) -> list[str]:
        # Bypass the manifest-registered predicate; use injected one.
        snapshot = self._load_snapshot(as_of)   # existing internal helper
        candidates = [self._predicate(meta, as_of) for meta in snapshot]
        return [s.symbol for s in candidates if s.matched and self.coverage_floor(s.symbol, as_of)]
```

Then `universe_recommender.js` calls `regime_blended_backtest.py` via the existing CLI runner with a `--resolver-override <predicate_name>` flag (new in Phase C) that constructs a `MockResolver` for that predicate.

### 2.5 Discord adoption flow

`src/channels/discord/bot.js` already has a reaction-handler dispatch for other Mastermind outputs (paper-curation approves, position-rec acks). Phase C adds a third pattern:

```js
// In existing reactionAdd handler, after existing dispatches:
if (message.embeds.length && message.embeds[0].footer?.text?.startsWith('universe-rec:')) {
  const recId = message.embeds[0].footer.text.split(':')[1];
  if (reaction.emoji.name === '✅') {
    await fetch(`http://127.0.0.1:7870/api/universe-recs/${recId}/approve`, {method:'POST'});
    await message.react('✔');
  } else if (reaction.emoji.name === '❌') {
    await fetch(`http://127.0.0.1:7870/api/universe-recs/${recId}/reject`, {method:'POST'});
    await message.react('✔');
  } else if (reaction.emoji.name === '⏸') {
    await fetch(`http://127.0.0.1:7870/api/universe-recs/${recId}/defer`, {method:'POST'});
    await message.react('✔');
  }
}
```

The dashboard endpoints invoke Python:
```python
# Behind /api/universe-recs/<id>/approve:
from src.strategies.lifecycle import adopt_universe_recommendation
adopt_universe_recommendation(rec_id)
```

### 2.6 `lifecycle.adopt_universe_recommendation`

```python
def adopt_universe_recommendation(rec_id: int) -> None:
    """Atomic adoption: DB write + manifest edit + audit log.

    Failure semantics: any step throws → full rollback. The manifest is
    written via a tmpfile + atomic rename; the DB transaction wraps the
    full operation including the rename via a two-phase pattern (write
    tmpfile, BEGIN, UPDATE table, fsync tmpfile, rename, COMMIT). If
    rename fails after COMMIT, an alert is fired and a recovery script
    re-syncs from DB to manifest.
    """
    with self._pg.cursor() as cur:
        cur.execute("SELECT strategy_id, candidate_predicate FROM strategy_universe_recommendations WHERE id=%s AND approved IS NULL", (rec_id,))
        row = cur.fetchone()
        if not row: raise ValueError(f"recommendation {rec_id} missing or already decided")
        strategy_id, candidate = row

        # Stage manifest edit
        manifest = json.load(open(MANIFEST))
        prior_ref = manifest['strategies'][strategy_id].get('metadata', {}).get('universe_filter_ref')
        manifest['strategies'][strategy_id].setdefault('metadata', {})['universe_filter_ref'] = candidate
        tmp = MANIFEST.with_suffix('.tmp')
        json.dump(manifest, open(tmp, 'w'), indent=2)
        os.fsync(open(tmp).fileno())

        # DB writes inside transaction
        cur.execute("UPDATE strategy_universe_recommendations SET approved=true, approved_at=NOW(), adopted=true, adopted_at=NOW() WHERE id=%s", (rec_id,))
        cur.execute("INSERT INTO lifecycle_audit_log (event, strategy_id, before_state, after_state, actor) VALUES ('universe_filter_adopted', %s, %s, %s, 'opus_universe_recs')",
                    (strategy_id, prior_ref, candidate))
        os.rename(tmp, MANIFEST)
        self._pg.commit()
```

The two-phase pattern is borrowed from the existing manifest-edit code in `lifecycle.transition`. **Critical regression risk** (per [[feedback-lifecycle-silent-strip]]): adding any new top-level field to manifest entries during this write requires lockstep updates to `StrategyRecord` + `from_manifest` + `to_dict`. Phase C does NOT add any new fields (only writes the existing `metadata.universe_filter_ref` which Phase A landed), so this risk is mitigated by design.

### 2.7 Doctor + system_checks

```
src/maintenance/doctor.py
  + _check_universe_recs_freshness (NOT slow; fast SQL)
    SELECT max(recommended_at) FROM strategy_universe_recommendations
    If gate ON + last run > 8 days → WARN; > 14 days → FAIL.

src/system_checks/checks/universe_recs_health.py
  @check(name='universe_recs_health', tags=['agents','strategies'], requires=['db'])
  Returns:
    PASS if last 4 weeks ≥ 3 runs and each run produced ≥ 30 recs
    WARN if last run produced < 30 recs or > 1 weekly skip in 4 weeks
    FAIL if no runs in 14 days while gate ON
```

### 2.8 Dashboard tile

```
src/channels/dashboard/server.js (:7870 operator)
  + GET /api/universe-recs
    Returns the latest run's per-strategy table:
      [{strategy_id, current_predicate, choice, confidence, expected_uplift,
        approved, adopted, rationale_excerpt, ...}]
    UI tile in Strategy Health row: "Universe Recommendations (this week)"
    with approve/reject buttons (bypass-Discord path for batch operator review).
```

---

## 3. Components

### 3.1 New files

```
src/agent/curators/universe_recommender.js                ~450 LoC
  Phase C driver. Mirrors comprehensive_review.js shape.
  Per strategy: _buildGrid, _packPrompt, runOneShot, _parseDecision, _persist, _post.

src/agent/prompts/subagents/universe-recommender.md       ~120 lines
  Opus prompt template (Section 2.2 above).

src/strategies/lifecycle_universe_adoption.py             ~200 LoC
  adopt_universe_recommendation(rec_id)
  revert_universe_recommendation(strategy_id, to_prior=True)
  list_pending_recommendations()
  CLI: python3 -m strategies.lifecycle_universe_adoption {adopt|revert|list}

src/strategies/universe_resolver.py                       MODIFY
  + class MockResolver(UniverseResolver) — see §2.4
  + UniverseResolver._load_snapshot helper exposed as protected (was private).

docs/universe-recs.service                                ~25 LoC
docs/universe-recs.timer                                  ~15 LoC
  Saturday 20:00 ET; runs run_mastermind.js --mode universe-recs.

src/system_checks/checks/universe_recs_health.py          ~70 LoC

tests/test_universe_recommender.py                        ~250 LoC
tests/test_universe_adoption.py                           ~200 LoC
tests/test_universe_recs_smoke.py                         ~120 LoC
```

### 3.2 Modified files

```
src/agent/curators/run_mastermind.js
  + Add --mode universe-recs branch + flag docs in header comment.
  + Add --strategy-id <X> --week <YYYY-MM-DD> for operator re-runs.

src/backtest/regime_blended_backtest.py
  + --resolver-override <candidate_name> CLI flag
  + Returns a richer JSON summary including the 8 metrics Phase C needs.

src/channels/discord/bot.js
  + Reaction handler for footer.text startsWith 'universe-rec:'.

src/channels/dashboard/server.js (:7870)
  + GET /api/universe-recs (latest week table)
  + POST /api/universe-recs/<id>/{approve,reject,defer}
  + UI panel "Universe Recommendations" with buttons.

src/maintenance/doctor.py
  + _check_universe_recs_freshness (gated on OPENCLAW_UNIVERSE_RECS)

src/system_checks/checks/__init__.py
  + from . import universe_recs_health
```

### 3.3 `.env` changes

```
ADD:
  OPENCLAW_UNIVERSE_RECS=0                  (kill switch / Phase C gate)
  UNIVERSE_RECS_WEEKLY_BUDGET_USD=400
  UNIVERSE_RECS_PER_STRATEGY_BUDGET_USD=8
  UNIVERSE_RECS_LOOKBACK_DAYS=365
  UNIVERSE_RECS_CANDIDATE_SET_VERSION=v1   (bumped when 12 candidates change)
  DISCORD_UNIVERSE_RECS_CHANNEL=universe-recs   (Discord channel name)
```

### 3.4 Schema

No new tables. Reuses Phase A's `strategy_universe_recommendations` (migration 112). Phase C adds `lifecycle_audit_log` rows (existing table) with `event='universe_filter_adopted'`.

If `lifecycle_audit_log` doesn't exist (verify against migrations directory at impl time), add it as **migration 116**:

```sql
-- 116_lifecycle_audit_log.sql (only if table doesn't exist)
CREATE TABLE IF NOT EXISTS lifecycle_audit_log (
  id            BIGSERIAL PRIMARY KEY,
  event         TEXT NOT NULL,
  strategy_id   TEXT NOT NULL,
  before_state  TEXT,
  after_state   TEXT,
  actor         TEXT NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lcal_strategy ON lifecycle_audit_log(strategy_id, occurred_at DESC);
```

### 3.5 Memory + docs updates

```
/root/.claude/projects/-root/memory/project_sp2_phase_c_universe_recs.md  (NEW)
  Mastermind universe-recs flow + 12-candidate set, Saturday 20:00 ET,
  Opus 4.7 1M, JSON schema, lifecycle.adopt_universe_recommendation contract.

MEMORY.md  index update.

CLAUDE.md "Recent Changes"  SP-2 Phase C entry post-deploy.

ARCHITECTURE.md  Per-Strategy Universe Resolution section extended with
  "Re-evaluation Loop (Mastermind universe-recs)" subsection.
```

---

## 4. Data Flow

### 4.1 Saturday Mastermind day (post Phase C)

```
10:00 ET  saturday-brain        (paper ingest + Tier-A backtest)
18:00 ET  comprehensive-review  (per-strategy memos)
19:00 ET  position-recs         (sizing recs from memos)
20:00 ET  universe-recs         (predicate proposals from candidate grid)  ← NEW
```

Budget cumulative: saturday-brain (~$200) + comprehensive-review (~$50) + position-recs (~$30) + universe-recs (~$13) = ~$293 weekly. Phase C's $400 weekly cap allows headroom for occasional re-runs.

### 4.2 Per-recommendation lifecycle

```
[Sat 20:00 ET]   universe_recommender produces 51 recs (one per live strategy)
                 INSERT INTO strategy_universe_recommendations
                 approved=NULL, adopted=false

[Sat 20:30 ET]   51 Discord messages posted to #universe-recs
                 (rate-limited: 5/sec to avoid Discord throttle)

[Sat 20:30+]     Operator reviews. For each:
                 ✅ → adopt_universe_recommendation(rec_id)
                      → UPDATE recs SET approved=true, adopted=true
                      → manifest.json edited (universe_filter_ref)
                      → lifecycle_audit_log row written

[Sun-Mon]        Adopted strategies' next live cycle picks up new predicate
                 via UniverseResolver reading manifest.universe_filter_ref.
                 No restart required.

[Following Sat]  universe_recommender skips strategies that adopted < 4 weeks ago
                 (avoids predicate thrash). Skips count toward "no_change"
                 in audit.
```

### 4.3 Operator-invoked re-run

```bash
# After fix to candidate set, force re-recommendation for one strategy:
node src/agent/curators/run_mastermind.js --mode universe-recs \
  --strategy-id S5_max_pain \
  --week 2026-09-13
```

Writes a new row; old row (if approved=NULL) is left as-is for audit. Operator can decide which to act on.

---

## 5. Phase Ordering Within Phase C

Phase C is a tight 5-task ship:

```
1. MockResolver + regime_blended_backtest CLI flag                    (Task 1-2)
2. universe_recommender.js + prompt template + run_mastermind.js wire (Task 3-4)
3. lifecycle.adopt_universe_recommendation + revert + tests           (Task 5)
4. Discord reaction handler + dashboard endpoints + tile              (Task 6)
5. Timer + .env + doctor + system_checks                              (Task 7)
6. Smoke + docs + memory                                              (Task 8)
7. PR + first supervised run                                          (Task 9)
```

---

## 6. Error Handling + Rollback

### 6.1 Failure-mode matrix

| Failure | Detection | Response | Severity |
|---|---|---|---|
| Backtest grid generation throws on one candidate | per-candidate try/except | That row in grid = `null`s; Opus prompt includes the failure note; Opus likely picks one of the working candidates. | LOW |
| Opus output malformed JSON | post-call schema validation | Reject recommendation; alert #universe-recs with raw output; no DB row written. | MEDIUM |
| Opus output picks a candidate not in the set | schema enum check | Same as malformed. | MEDIUM |
| Opus budget cap hit mid-run | running cost tracker | Halt cleanly after current strategy; partial result preserved; resume next Saturday. | LOW |
| Discord post throttled (429) | retry with backoff | Up to 5 retries with exponential backoff; if still failing, write to dashboard tile only and skip Discord. | LOW |
| Reaction handler can't reach dashboard | network/process down | Reaction emoji is logged; operator can run `python3 -m strategies.lifecycle_universe_adoption adopt --rec-id <X>` manually. | LOW |
| `adopt_universe_recommendation` fails mid-write (DB committed but manifest rename failed) | Post-commit fsync + rename pattern; alert on rename exception | Recovery script `scripts/sync_manifest_from_db.py` rebuilds manifest from `strategy_universe_recommendations.adopted=true` rows. | HIGH |
| Adopted predicate breaks the strategy's resolution (empty universe) | resolver returns empty; downstream signals=0 | Strategy gets no orders that day; alert; operator runs `revert_universe_recommendation`. | MEDIUM |
| Mastermind cost exceeds $8/strategy | per-strategy circuit-breaker | Skip remaining strategies; alert; weekly budget logged. | LOW |
| Candidate set version mismatch | `UNIVERSE_RECS_CANDIDATE_SET_VERSION` env check at startup | Driver refuses to start if env != src/strategies/universe_default.py declared version. | LOW |

### 6.2 Rollback ladder

```
LEVEL 1 — Kill recommender, freeze any pending recs
  OPENCLAW_UNIVERSE_RECS=0
  systemctl stop openclaw-universe-recs.timer
  Effect: no new recs; existing approved+adopted predicates stay in effect.
  Wall: ≤30s.

LEVEL 2 — Revert a single adopted predicate
  python3 -m strategies.lifecycle_universe_adoption revert --strategy-id <X>
  Restores prior universe_filter_ref from lifecycle_audit_log.
  Wall: ≤5s.

LEVEL 3 — Revert ALL adopted predicates back to default (sp500)
  python3 -m strategies.lifecycle_universe_adoption revert --all
  Sets every strategy's universe_filter_ref to None (→ default).
  Wall: ~30s; live cycle picks up next iteration.

LEVEL 4 — Full Phase C revert
  git revert <Phase-C-merge-SHA>
  Migration 116 (if added) stays — append-only.
  recs table stays with adopted=true rows; ignored by post-revert code.
  Adopted predicates STAY in manifest (Phase A still reads them).
  To also wipe them: run Level 3 BEFORE git revert.
  Wall: ~15min.
```

### 6.3 Pre-deploy operator checklist

```
[ ] Phase B Soak B passed; ticker_metadata_history_depth = PASS
[ ] PR feat/sp2-phase-c-mastermind-universe-recs merged
[ ] OPENCLAW_UNIVERSE_RECS=0 at first (timer dry-run only)
[ ] DISCORD_UNIVERSE_RECS_CHANNEL created in Discord
[ ] First Saturday run: --dry-run flag set, validate grid generation across 5 random strategies
[ ] Second Saturday run: live but operator-supervised (every rec reviewed before any approve)
[ ] After 2 successful Saturdays + ≥ 5 strategies adopted, OPENCLAW_UNIVERSE_RECS=1 stays ON
[ ] Weekly budget tracked; alert if > $350 (cap is $400)
```

---

## 7. Testing + Validation

### 7.1 Unit tests

```
tests/test_universe_recommender.py
  - _buildGrid produces 12-row deterministic grid for fixture strategy
  - grid_sha256 stable across runs with same inputs
  - prompt assembly handles thesis/source code escaping (no double-render)
  - JSON parser rejects malformed; rejects unknown candidate; accepts valid
  - cost tracker stops dispatch at per-strategy cap
  - skip-recent-adoption logic (< 4 weeks) works

tests/test_universe_adoption.py
  - adopt writes DB + manifest + audit row atomically
  - DB commit + rename failure → recovery script restores invariant
  - revert restores prior universe_filter_ref
  - revert --all clears every strategy's ref
  - cannot adopt already-decided rec (idempotency)

tests/test_resolver_mock.py
  - MockResolver bypasses manifest predicate
  - coverage_floor still applied
  - regime_blended_backtest --resolver-override produces different
    grid for different candidates (parametrized over 3 candidates)
```

### 7.2 Integration: tests/test_universe_recs_smoke.py

```
1. Dry-run dispatcher:
   node src/agent/curators/run_mastermind.js --mode universe-recs --dry-run --strategy-id S5_max_pain
   - Builds grid (no DB write); no Opus call (--dry-run path);
     prints prompt + grid SHA.

2. End-to-end on one strategy (uses real Opus call, ~$0.25):
   ENV=staging node src/agent/curators/run_mastermind.js --mode universe-recs --strategy-id S5_max_pain
   - Writes 1 row to strategy_universe_recommendations
   - Posts 1 message to #universe-recs (or stdout if Discord webhook unset)
   - Cost rotation logged

3. Manual adopt:
   python3 -m strategies.lifecycle_universe_adoption adopt --rec-id <id>
   - manifest updated; audit log row; recs row adopted=true

4. Revert:
   python3 -m strategies.lifecycle_universe_adoption revert --strategy-id S5_max_pain
   - manifest restored to prior ref; audit log row written

5. system_checks --check universe_recs_health → PASS (or WARN if no real runs yet)
```

### 7.3 Pre-deploy soak

**Soak A: code-deploy + dry-run** — 1 week.
- Timer fires on Saturday with `--dry-run` flag (no DB writes, no Opus spend).
- Grid generation works for all 51 live strategies.
- Total dry-run runtime < 30 minutes.

**Soak B: first live run, operator-supervised** — 1 Saturday.
- Operator reviews all 51 recommendations before clicking any approve.
- Adopts 5–10 high-confidence recs to verify lifecycle write + next-day cycle.

**Soak C: 4 weekly runs unsupervised** — 4 weeks.
- Weekly budget < $400.
- ≥ 30 recs per run.
- ≥ 1 strategy adopted per run on average.
- No `lifecycle_audit_log` rollback events.

### 7.4 Out of scope for Phase C

- Free-form predicate emission by Opus (would need its own lint+sandbox).
- Multi-strategy correlated predicate selection (e.g., "spread these 3 strategies across orthogonal slices").
- Adversarial predicate testing (e.g., "what's the worst slice for this strategy").
- Predicate change retroactive backfill of past trades (predicates affect future only).
- PaperHunter / StrategyCoder predicate-at-creation (Phase D).

---

## 8. References

- Parent spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.3 + §7.5
- Phase A: `src/strategies/universe_resolver.py`, `src/strategies/universe_default.py`, migration 112 (`strategy_universe_recommendations`).
- Phase B: `docs/superpowers/specs/2026-05-22-sp2-phase-b-5y-backfill-design.md` (precondition: ≥ 5y `ticker_metadata_snapshots` depth).
- Existing Mastermind curator pattern: `src/agent/curators/comprehensive_review.js`, `src/agent/curators/_opus_oneshot.js`.
- Dispatcher: `src/agent/curators/run_mastermind.js`.
- Discord reaction handler entry-point: `src/channels/discord/bot.js`.
- Memory: `feedback_lifecycle_silent_strip.md` (manifest top-level field discipline — Phase C does NOT add fields).
- Memory: `feedback_universe_predicate_contract.md` (Phase A contract — Phase C only switches `universe_filter_ref` between Phase A-vetted predicates; doesn't bypass the contract).
