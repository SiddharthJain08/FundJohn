# SP-2 Phase D: Research-Hooks for Predicate Emission — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.

**Goal:** PaperHunter infers a universe predicate at extraction time; StrategyCoder emits the predicate import at file-creation time; lifecycle propagates it into `manifest.metadata.universe_filter_ref` at staging time. Net-new strategies land with an explicit slice instead of waiting for Phase C's Saturday cycle to discover it.

**Architecture:** Prompt-level extension to two existing subagent prompts (PaperHunter + StrategyCoder) + a small orchestrator validator + a small lifecycle helper. No schema changes; the new field lives inside the existing `research_candidates.hunter_result_json` JSONB blob. Whitelist enforcement against `CANDIDATE_PREDICATES` keeps emission constrained to the 12 vetted slices.

**Tech Stack:** Markdown (prompt edits); JavaScript (orchestrator validator); Python 3.13 (lifecycle helper, system_check, tests); no Postgres migrations; no systemd unit changes.

**Spec:** `docs/superpowers/specs/2026-05-22-sp2-phase-d-research-hooks-design.md`

**Branch:** `feat/sp2-phase-d-research-hooks` (off main, after Phase C ships its first adoption cycle: ≥ 5 strategies on explicit predicates)

**Acceptance:**
- PaperHunter prompt has the new §5 "Infer universe slice" section + JSON output field.
- StrategyCoder prompt explains when to add `universe_filter` import.
- Orchestrator validates `inferred_universe_filter` against the whitelist and falls back to null on invalid.
- `lifecycle.stage(file)` detects module-scope `universe_filter` import and registers `manifest.metadata.universe_filter_ref`.
- First post-deploy saturday-brain cycle: ≥ 50% of new candidates emit a non-null inferred predicate.
- Soft-rollback works: `OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0` reverts behavior without redeploy.

---

## ⚠️ Codebase conventions

Same Phase A/B/C substitutions apply (POSTGRES_URI, psycopg2, node migrate, system_check layout + tuple returns).

**Phase D-specific conventions:**

| In the plan | Use instead | Source |
|---|---|---|
| Where to detect the StrategyCoder invocation site | `src/agent/research/research-orchestrator.js` — verify exact function name (likely `_runStrategyCoder` or `_runCoder`) at impl time | Grep `'strategycoder' src/agent/research/` |
| How orchestrator passes context to subagent prompts | Look for existing `promptCtx` / `template substitution` pattern in `research-orchestrator.js` and mirror it | match existing style |
| Lifecycle stage entry point | `python3 -m src.strategies.lifecycle stage --strategy-id <X> --file <path>` — verify subcommand name | `python3 -m src.strategies.lifecycle --help` |
| Manifest path | `src/strategies/manifest.json` (Phase C confirmed) | already validated |
| StrategyRecord field discipline | DO NOT add new top-level fields | `feedback_lifecycle_silent_strip.md` |
| Where `CANDIDATE_PREDICATES` is defined | `src/strategies/universe_default.py` — Phase A | already established |

---

## Task 0: Branch + workspace setup

**Files:** none

- [ ] **Step 1: Confirm Phase C is settled**

```bash
cd /root/openclaw && git fetch origin
git log origin/main --oneline | grep -i "sp-2 phase c" | head -3
gh pr view <phase-c-pr#> --json state -q .state    # MERGED
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -c "
import os, psycopg2; c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute(\"SELECT count(*) FROM strategy_universe_recommendations WHERE adopted=true\")
print('adopted strategies via Phase C:', cur.fetchone()[0])
"
# expect ≥ 5
```

- [ ] **Step 2: Create feature branch**

```bash
git checkout main && git pull
git checkout -b feat/sp2-phase-d-research-hooks origin/main
git status   # clean
```

- [ ] **Step 3: Verify orchestrator + lifecycle entry points**

```bash
grep -n "strategycoder\|StrategyCoder\|_runStrategyCoder\|_runCoder" src/agent/research/research-orchestrator.js | head -10
python3 -m src.strategies.lifecycle --help 2>&1 | head -30
ls src/agent/prompts/subagents/ | grep -E "paperhunter|strategycoder"
```

---

## Task 1: PaperHunter prompt extension

**Files:**
- Modify: `src/agent/prompts/subagents/paperhunter.md`

- [ ] **Step 1: Author the new §5 section**

Insert after Step 4 (gates) and before the JSON output section, using the spec §2.2 content verbatim.

- [ ] **Step 2: Extend the JSON schema doc** so `inferred_universe_filter` appears as an expected top-level field.

- [ ] **Step 3: Static test**

```python
# tests/test_paperhunter_prompt_static.py
from pathlib import Path
def test_paperhunter_has_phase_d_section():
    p = Path('src/agent/prompts/subagents/paperhunter.md').read_text()
    assert '## Step 5 — Infer universe slice' in p
    assert 'inferred_universe_filter' in p
    # 12 candidates listed (smoke check)
    for name in ['sp500','large_cap','options_eligible','tech_sector']:
        assert name in p
```

- [ ] **Step 4: Run + commit**

```bash
python3 -m pytest tests/test_paperhunter_prompt_static.py -v
git add src/agent/prompts/subagents/paperhunter.md tests/test_paperhunter_prompt_static.py
git commit -m "feat(sp2-d): PaperHunter prompt §5 — infer universe slice"
```

---

## Task 2: StrategyCoder prompt extension

**Files:**
- Modify: `src/agent/prompts/subagents/strategycoder.md`

- [ ] **Step 1: Add "Universe predicate" section**

Insert after "Required Artifacts → Artifact 1 — Implementation file" using the spec §2.3 content.

- [ ] **Step 2: Static test**

```python
# tests/test_strategycoder_prompt_static.py
from pathlib import Path
def test_strategycoder_has_phase_d_section():
    p = Path('src/agent/prompts/subagents/strategycoder.md').read_text()
    assert 'Universe predicate' in p
    assert 'INFERRED_UNIVERSE_FILTER' in p
    assert 'universe_default import' in p
```

- [ ] **Step 3: Run + commit**

```bash
python3 -m pytest tests/test_strategycoder_prompt_static.py -v
git add src/agent/prompts/subagents/strategycoder.md tests/test_strategycoder_prompt_static.py
git commit -m "feat(sp2-d): StrategyCoder prompt — universe predicate emission rules"
```

---

## Task 3: Orchestrator validator + prompt-context injection

**Files:**
- Modify: `src/agent/research/research-orchestrator.js`
- Create: `tests/test_orchestrator_predicate_injection.js` (if a node test pattern exists; otherwise replicate via a python subprocess test)

- [ ] **Step 1: Add `_validateInferredFilter`**

```js
// Add near other private helpers in research-orchestrator.js
const { spawnSync } = require('child_process');
const PYTHON = process.env.PYTHON_BIN || 'python3';

function _validateInferredFilter(name) {
  if (name == null) return null;
  if (process.env.OPENCLAW_PHASE_D_PREDICATE_AT_MINT !== '1') return null;
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

- [ ] **Step 2: Wire into StrategyCoder invocation**

Find the StrategyCoder dispatch site (verified in Task 0 Step 3) and inject:

```js
const inferred = candidate.hunter_result_json?.inferred_universe_filter ?? null;
const validInferred = _validateInferredFilter(inferred);
const promptCtx = {
  ...existingCtx,
  INFERRED_UNIVERSE_FILTER: validInferred,  // null or one of 12 names
};
```

- [ ] **Step 3: Tests**

```python
# tests/test_orchestrator_predicate_injection.py (subprocess-based; node-test)
import subprocess

VALIDATE_SNIPPET = """
const { spawnSync } = require('child_process');
const PYTHON = 'python3';
function _validateInferredFilter(name) {
  if (name == null) return null;
  if (process.env.OPENCLAW_PHASE_D_PREDICATE_AT_MINT !== '1') return null;
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.universe_default import CANDIDATE_PREDICATES; '
    + 'import sys; sys.exit(0 if sys.argv[1] in CANDIDATE_PREDICATES else 1)',
    name], {encoding:'utf8'});
  return r.status === 0 ? name : null;
}
console.log(JSON.stringify({
  good: _validateInferredFilter('sp500'),
  bad: _validateInferredFilter('NOT_A_PREDICATE'),
  null_in: _validateInferredFilter(null),
}));
"""

def test_validate_valid_name(monkeypatch):
    r = subprocess.run(['node','-e', VALIDATE_SNIPPET],
                       capture_output=True, text=True,
                       env={'OPENCLAW_PHASE_D_PREDICATE_AT_MINT':'1','PATH':'/usr/bin:/usr/local/bin'})
    assert r.returncode == 0
    import json
    out = json.loads(r.stdout.strip())
    assert out['good'] == 'sp500'
    assert out['bad'] is None
    assert out['null_in'] is None

def test_gate_off_drops_everything():
    r = subprocess.run(['node','-e', VALIDATE_SNIPPET],
                       capture_output=True, text=True,
                       env={'PATH':'/usr/bin:/usr/local/bin'})  # gate unset
    import json
    out = json.loads(r.stdout.strip())
    assert out['good'] is None     # dropped because gate off
```

- [ ] **Step 4: Run + commit**

```bash
node -c src/agent/research/research-orchestrator.js
python3 -m pytest tests/test_orchestrator_predicate_injection.py -v
git add src/agent/research/research-orchestrator.js tests/test_orchestrator_predicate_injection.py
git commit -m "feat(sp2-d): orchestrator validates inferred_universe_filter + injects into coder ctx"
```

---

## Task 4: Lifecycle helper + predicate detection

**Files:**
- Modify: `src/strategies/lifecycle.py`
- Create: `tests/test_lifecycle_predicate_detection.py`

- [ ] **Step 1: Add `_detect_module_predicate` + `register_strategy_predicate`**

In `lifecycle.py`, add (NOT as `StrategyRecord` fields — those stay unchanged):

```python
import ast

def _detect_module_predicate(file_path: Path) -> str | None:
    """Parse strategy file's AST. If it has
       `from src.strategies.universe_default import <name> as universe_filter`
       at module scope, return <name>. Else None.
    """
    try:
        tree = ast.parse(file_path.read_text())
    except (FileNotFoundError, SyntaxError):
        return None
    for node in tree.body:   # only top-level
        if isinstance(node, ast.ImportFrom) and node.module in (
            'src.strategies.universe_default', 'strategies.universe_default'):
            for alias in node.names:
                if alias.asname == 'universe_filter':
                    return alias.name
    return None

class LifecycleStateMachine:
    ...
    def register_strategy_predicate(self, strategy_id: str, predicate_name: str | None) -> None:
        from src.strategies.universe_default import CANDIDATE_PREDICATES
        if predicate_name is not None and predicate_name not in CANDIDATE_PREDICATES:
            raise ValueError(f"predicate '{predicate_name}' not in candidate set")
        manifest = json.loads(MANIFEST.read_text())
        entry = manifest['strategies'].get(strategy_id)
        if not entry:
            raise ValueError(f"strategy {strategy_id} not in manifest")
        entry.setdefault('metadata', {})
        if predicate_name:
            entry['metadata']['universe_filter_ref'] = predicate_name
        else:
            entry['metadata'].pop('universe_filter_ref', None)
        tmp = MANIFEST.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, indent=2))
        os.fsync(open(tmp).fileno())
        os.rename(tmp, MANIFEST)
```

- [ ] **Step 2: Wire into `stage()`**

In the existing `stage()` method, after the strategy file is copied into `implementations/` and added to the manifest, add (gated):

```python
if os.environ.get('OPENCLAW_PHASE_D_PREDICATE_AT_MINT') == '1':
    detected = _detect_module_predicate(final_file_path)
    if detected:
        self.register_strategy_predicate(strategy_id, detected)
```

- [ ] **Step 3: Tests**

```python
# tests/test_lifecycle_predicate_detection.py
import pytest, json, os
from pathlib import Path
from src.strategies.lifecycle import _detect_module_predicate, LifecycleStateMachine, MANIFEST

WITH_IMPORT = """
from src.strategies.base import BaseStrategy
from src.strategies.universe_default import large_cap as universe_filter

class TestStrategy(BaseStrategy):
    def generate_signals(self, prices, regime, universe, aux_data=None): return []
"""

WITHOUT_IMPORT = """
from src.strategies.base import BaseStrategy
class TestStrategy(BaseStrategy):
    def generate_signals(self, prices, regime, universe, aux_data=None): return []
"""

WITH_IMPORT_NO_ALIAS = """
from src.strategies.universe_default import large_cap
from src.strategies.base import BaseStrategy
class TestStrategy(BaseStrategy):
    def generate_signals(self, prices, regime, universe, aux_data=None): return []
"""

def test_detect_picks_up_aliased_import(tmp_path):
    p = tmp_path / 's.py'; p.write_text(WITH_IMPORT)
    assert _detect_module_predicate(p) == 'large_cap'

def test_detect_returns_none_without_import(tmp_path):
    p = tmp_path / 's.py'; p.write_text(WITHOUT_IMPORT)
    assert _detect_module_predicate(p) is None

def test_detect_returns_none_when_not_aliased(tmp_path):
    p = tmp_path / 's.py'; p.write_text(WITH_IMPORT_NO_ALIAS)
    assert _detect_module_predicate(p) is None

def test_register_rejects_unknown_predicate():
    lsm = LifecycleStateMachine()
    with pytest.raises(ValueError, match='candidate set'):
        lsm.register_strategy_predicate('S5_max_pain', 'definitely_not_a_predicate')

def test_register_writes_manifest(tmp_path, monkeypatch):
    # Use a temp manifest copy to avoid mutating prod state
    monkeypatch.setattr('src.strategies.lifecycle.MANIFEST', tmp_path / 'manifest.json')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'strategies': {'S_test': {'name':'t','metadata':{}}}
    }))
    lsm = LifecycleStateMachine()
    lsm.register_strategy_predicate('S_test', 'large_cap')
    m = json.loads((tmp_path / 'manifest.json').read_text())
    assert m['strategies']['S_test']['metadata']['universe_filter_ref'] == 'large_cap'
    lsm.register_strategy_predicate('S_test', None)
    m = json.loads((tmp_path / 'manifest.json').read_text())
    assert 'universe_filter_ref' not in m['strategies']['S_test']['metadata']
```

- [ ] **Step 4: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_lifecycle_predicate_detection.py -v
git add src/strategies/lifecycle.py tests/test_lifecycle_predicate_detection.py
git commit -m "feat(sp2-d): lifecycle _detect_module_predicate + register_strategy_predicate"
```

---

## Task 5: System_check + dashboard tile

**Files:**
- Create: `src/system_checks/checks/papermint_predicate_coverage.py`
- Modify: `src/system_checks/checks/__init__.py`
- Modify: `src/channels/dashboard/server.js`
- Modify: `src/channels/dashboard/public/index.html`
- Create: `tests/test_papermint_coverage_check.py`

- [ ] **Step 1: system_check**

```python
# src/system_checks/checks/papermint_predicate_coverage.py
from src.system_checks.registry import check
from src.system_checks.status import Status

@check(name='papermint_predicate_coverage', tags=['agents','strategies'], requires=['db'])
def run() -> tuple[Status, str]:
    import os, psycopg2
    if os.environ.get('OPENCLAW_PHASE_D_PREDICATE_AT_MINT') != '1':
        return Status.PASS, 'gate off; n/a'
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""SELECT
              count(*) FILTER (WHERE hunter_result_json->>'inferred_universe_filter' IS NOT NULL AND hunter_result_json->>'inferred_universe_filter' != 'null'),
              count(*) FILTER (WHERE hunter_result_json IS NOT NULL)
          FROM research_candidates
          WHERE submitted_at > NOW() - INTERVAL '30 days'""")
        with_pred, total = cur.fetchone()
    pg.close()
    if total < 5:  return Status.PASS, f'too few new candidates ({total}) — n/a'
    pct = 100.0 * with_pred / total
    if pct == 0:        return Status.FAIL, '0% of 30d candidates emitted predicate (prompt regression?)'
    if pct < 20:        return Status.WARN, f'only {pct:.0f}% predicate coverage in 30d'
    return Status.PASS, f'{pct:.0f}% coverage ({with_pred}/{total})'
```

- [ ] **Step 2: Register**

```python
# src/system_checks/checks/__init__.py — append
from . import papermint_predicate_coverage   # noqa: F401
```

- [ ] **Step 3: Dashboard tile**

```js
// src/channels/dashboard/server.js — add endpoint
app.get('/api/papermint-recent', async (req, res) => {
  const pool = req.app.locals.pool;
  const { rows } = await pool.query(`
    SELECT rc.candidate_id, rc.source_url, rc.submitted_at,
           rc.hunter_result_json->>'inferred_universe_filter' AS inferred,
           sr.id AS staged_strategy_id,
           sr.created_at - rc.submitted_at AS adoption_lag
    FROM research_candidates rc
    LEFT JOIN strategy_registry sr
      ON sr.id = rc.hunter_result_json->>'strategy_id'
    WHERE rc.submitted_at > NOW() - INTERVAL '30 days'
    ORDER BY rc.submitted_at DESC LIMIT 50`);
  res.json(rows);
});
```

```html
<!-- src/channels/dashboard/public/index.html — add card -->
<div class="card span-6" id="papermintCard">
  <h3>Recent net-new strategies (predicate at mint)</h3>
  <div id="papermintTable">Loading…</div>
</div>
<script>
async function refreshPapermint() {
  const rows = await fetch('/api/papermint-recent').then(r=>r.json());
  const html = rows.map(r=>`<tr><td>${r.candidate_id.slice(0,8)}</td><td>${r.inferred||'(default)'}</td><td>${r.staged_strategy_id||'pending'}</td></tr>`).join('');
  document.getElementById('papermintTable').innerHTML =
    `<table><thead><tr><th>Cand</th><th>Predicate</th><th>Staged</th></tr></thead><tbody>${html}</tbody></table>`;
}
refreshPapermint(); setInterval(refreshPapermint, 60000);
</script>
```

- [ ] **Step 4: Tests**

```python
# tests/test_papermint_coverage_check.py
import subprocess
def test_check_gate_off_passes(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PHASE_D_PREDICATE_AT_MINT', raising=False)
    r = subprocess.run(['python3','-m','src.system_checks','--check','papermint_predicate_coverage','--json'],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert 'gate off' in r.stdout or 'PASS' in r.stdout
```

- [ ] **Step 5: Run + commit**

```bash
python3 -m pytest tests/test_papermint_coverage_check.py -v
node -c src/channels/dashboard/server.js
git add src/system_checks/checks/papermint_predicate_coverage.py src/system_checks/checks/__init__.py
git add src/channels/dashboard/server.js src/channels/dashboard/public/index.html tests/test_papermint_coverage_check.py
git commit -m "feat(sp2-d): system_check papermint_predicate_coverage + dashboard tile"
```

---

## Task 6: Smoke + docs + memory

**Files:**
- Create: `tests/test_phase_d_smoke.py`
- Create: `docs/sp2-papermint-runbook.md`
- Modify: `.env.example`, `CLAUDE.md`, `ARCHITECTURE.md`
- Create: `/root/.claude/projects/-root/memory/project_sp2_phase_d_research_hooks.md`
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`

- [ ] **Step 1: Smoke test**

```python
# tests/test_phase_d_smoke.py
import subprocess, json
from pathlib import Path

def test_both_prompts_have_phase_d_sections():
    ph = Path('src/agent/prompts/subagents/paperhunter.md').read_text()
    sc = Path('src/agent/prompts/subagents/strategycoder.md').read_text()
    assert '## Step 5 — Infer universe slice' in ph
    assert 'Universe predicate' in sc

def test_lifecycle_module_detect_smoke(tmp_path):
    from src.strategies.lifecycle import _detect_module_predicate
    f = tmp_path / 's.py'
    f.write_text('from src.strategies.universe_default import sp500 as universe_filter\n')
    assert _detect_module_predicate(f) == 'sp500'

def test_orchestrator_validator_smoke():
    r = subprocess.run(['node','-e','''
      const { spawnSync } = require("child_process");
      const r = spawnSync("python3", ["-c", "from src.strategies.universe_default import CANDIDATE_PREDICATES; import sys; sys.exit(0 if sys.argv[1] in CANDIDATE_PREDICATES else 1)", "sp500"], {encoding:"utf8"});
      console.log(r.status);
    '''], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == '0'
```

- [ ] **Step 2: Runbook**

```markdown
# /root/openclaw/docs/sp2-papermint-runbook.md

# SP-2 Phase D — Predicate-at-mint Runbook

## What changed
PaperHunter now infers one of 12 universe predicates per paper; StrategyCoder writes the import; lifecycle registers it in manifest.

## How to validate this Saturday's batch
1. After saturday-brain completes, query:
   ```sql
   SELECT candidate_id, hunter_result_json->>'inferred_universe_filter' AS pred,
          left(hunter_result_json->>'strategy_thesis', 80) AS thesis
   FROM research_candidates
   WHERE submitted_at > NOW() - INTERVAL '24 hours';
   ```
2. Eyeball each (pred, thesis) pair. ≥ 80% sane → leave gate on.
3. < 50% sane or ≥ 1 obviously absurd pick (e.g., crypto thesis → tech_sector) → Level 1 rollback.

## Rollback
- Level 1 (soft): `OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0` + restart johnbot.service
- Level 2 (per-strategy): `python3 -m src.strategies.lifecycle_universe_adoption revert --strategy-id <X>`
- Level 3 (full): `git revert <Phase-D-merge-SHA>`. Existing minted strategies keep their explicit predicate; Level 2 also needed if you want to strip them.

## When to re-enable after rollback
After patching the prompt or whitelist; first re-enable requires operator-supervised saturday-brain run.
```

- [ ] **Step 3: `.env.example`**

```
# SP-2 Phase D — Predicate-at-mint
OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1
```

- [ ] **Step 4: CLAUDE.md / ARCHITECTURE.md / memory**

```markdown
# /root/.claude/projects/-root/memory/project_sp2_phase_d_research_hooks.md
---
name: project-sp2-phase-d-research-hooks
description: "SP-2 Phase D shipped. PaperHunter §5 infers universe predicate (whitelist of 12); StrategyCoder writes the import; lifecycle.stage detects it via AST + registers manifest.metadata.universe_filter_ref. Gate OPENCLAW_PHASE_D_PREDICATE_AT_MINT (default ON). No DB schema change — field rides hunter_result_json JSONB."
metadata: {node_type: memory, type: project}
---

...
```

Update MEMORY.md index; CLAUDE.md Recent Changes entry; ARCHITECTURE.md new subsection.

- [ ] **Step 5: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_phase_d_smoke.py -v
git add tests/test_phase_d_smoke.py docs/sp2-papermint-runbook.md .env.example CLAUDE.md ARCHITECTURE.md
git add /root/.claude/projects/-root/memory/project_sp2_phase_d_research_hooks.md
git add /root/.claude/projects/-root/memory/MEMORY.md
git commit -m "docs(sp2-d): smoke + runbook + memory + CLAUDE/ARCHITECTURE updates"
```

---

## Task 7: PR + supervised first PaperHunter run

- [ ] **Step 1: Full local sweep**

```bash
python3 -m pytest tests/ --ignore=tests/integration_test.py -x --tb=short 2>&1 | tail -50
node test/graph-smoke.js
node test/paperhunter-smoke.js
python3 -m src.system_checks
```

- [ ] **Step 2: Push + open PR**

```bash
git push -u origin feat/sp2-phase-d-research-hooks
gh pr create --base main --head feat/sp2-phase-d-research-hooks \
  --title "SP-2 Phase D: predicate-at-mint via PaperHunter §5 + StrategyCoder + lifecycle" \
  --body "$(cat <<'EOF'
## Summary
- PaperHunter prompt gains §5 "Infer universe slice" — picks one of the 12 Phase A candidates (or null).
- StrategyCoder prompt gains "Universe predicate" section explaining the import to emit (or omit) per the orchestrator-injected INFERRED_UNIVERSE_FILTER context.
- Orchestrator validates the field against CANDIDATE_PREDICATES whitelist; falls back to null on invalid/unknown.
- lifecycle.stage detects module-scope `from src.strategies.universe_default import <name> as universe_filter` via AST and calls register_strategy_predicate(strategy_id, name) → manifest.metadata.universe_filter_ref.
- New system_check papermint_predicate_coverage on agents+strategies tag.
- Operator dashboard tile "Recent net-new strategies (predicate at mint)".
- NO schema migrations; the new field lives inside research_candidates.hunter_result_json (JSONB).
- Gate OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1 (default ON; kill switch flips behavior back to pre-Phase-D).

Spec: docs/superpowers/specs/2026-05-22-sp2-phase-d-research-hooks-design.md
Plan: docs/superpowers/plans/2026-05-22-sp2-phase-d-research-hooks.md

## Test plan
- [ ] pytest tests/ green (SP-2 + regression baseline)
- [ ] node smokes green
- [ ] First post-deploy saturday-brain: operator inspects 10 random hunter_result_json blobs; ≥ 80% sane → leave on
- [ ] If < 50% sane: OPENCLAW_PHASE_D_PREDICATE_AT_MINT=0 immediately, root-cause prompt, re-deploy

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Operator deploy + supervised Saturday**

```bash
ssh vps
cd /root/openclaw && git checkout main && git pull
systemctl restart johnbot.service
# First Saturday after deploy: monitor #strategy-memos and run the validation
# query from docs/sp2-papermint-runbook.md
```

- [ ] **Step 4: Soak**

Per spec §6.3 + §7.3. Single Saturday gates; not a multi-week soak.

---

## Out of Scope for Phase D

- Free-form predicate emission (forever out of scope).
- Retroactive inference on past `research_candidates` (Phase C handles).
- Multi-predicate-per-strategy.
- Cross-paper predicate sharing.
- StrategyCoder generating new candidate predicates.

---

## Spec coverage cross-check

| Spec § | Topic | Task(s) |
|---|---|---|
| 1.2 | Decisions locked | All tasks |
| 2.1 | End-to-end flow | Tasks 1-4 |
| 2.2 | PaperHunter prompt extension | Task 1 |
| 2.3 | StrategyCoder prompt extension | Task 2 |
| 2.4 | Orchestrator wiring | Task 3 |
| 2.5 | Lifecycle helper | Task 4 |
| 2.6 | Doctor / system_checks | Task 5 |
| 2.7 | Dashboard tile | Task 5 |
| 3.1 | New files | Tasks 5, 6 |
| 3.2 | Modified files | Tasks 1-5 |
| 3.3 | .env changes | Task 6 |
| 3.5 | Memory + docs | Task 6 |
| 4 | Data flow | Tasks 1-4 |
| 6.1 | Failure-mode matrix | Tasks 3-4 (validator + lint integration) |
| 6.2 | Rollback ladder | Task 6 (runbook + env gate) |
| 6.3 | Pre-deploy checklist | Task 7 |
| 7 | Tests | each task + Task 6 smoke |
