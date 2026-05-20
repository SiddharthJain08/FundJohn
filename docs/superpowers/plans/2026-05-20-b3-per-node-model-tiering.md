# B3 — Per-Node Model Tiering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize per-node LLM selection so curators can ask for an opus-tier judge or sonnet-tier debator without hard-coding model IDs. Backward-compatible: subagents without tier config resolve to today's `SUBAGENT_MODELS[type].model`.

**Architecture:** New `resolve_model.js` helper that reads from two places: `subagent-types.json` (per-subagent `model_tiers` and `model_modes.<mode>.node_models`) plus `models.js::SUBAGENT_MODELS` as the default fallback. All gated behind a default-OFF env flag `OPENCLAW_MODEL_TIERING=1`. F3's later plan consumes this helper.

**Tech Stack:** Node 20 (native `node:test`), JSON config.

---

## File structure

| Path | Responsibility |
|---|---|
| `src/agent/config/resolve_model.js` (new) | The resolution helper — reads tiers + node overrides, falls back to defaults |
| `tests/test_resolve_model.test.js` (new) | Unit tests covering all six resolution paths |
| `src/agent/config/subagent-types.json` (modify) | Add `model_tiers` + `model_modes` to the `mastermind` entry only (other subagents wait) |
| `src/agent/config/models.js` (modify) | Add `OPENCLAW_MODEL_TIERING` constant; no behavioral change |
| `src/agent/curators/comprehensive_review.js` (modify) | Proof-of-life call site: switch `memo_writer` LLM selection to `resolveModel()` |

---

## Task 1: Write failing tests for resolveModel()

**Files:**
- Test: `tests/test_resolve_model.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const { resolveModel, _setConfigForTests } = require(path.join(ROOT, 'src/agent/config/resolve_model.js'));

const FIXTURE_TYPES = {
  defaults: {},
  types: {
    mastermind: {
      model_tiers: {
        judge:       'claude-opus-4-7',
        synthesizer: 'claude-opus-4-7',
        debator:     'claude-sonnet-4-6',
      },
      model_modes: {
        'comprehensive-review': {
          node_models: { memo_writer: 'judge' },
        },
        critique: {
          node_models: {
            aggressive_critic: 'debator',
            literal_model:     'claude-haiku-4-5-20251001',  // literal id, not a tier
          },
        },
      },
    },
    paperhunter: {
      // No model_tiers / model_modes — must fall back to SUBAGENT_MODELS
    },
  },
};

const FIXTURE_SUBAGENT_MODELS = {
  mastermind:  { model: 'claude-opus-4-7-default' },
  paperhunter: { model: 'claude-sonnet-4-6' },
};

test.beforeEach(() => {
  _setConfigForTests({ subagentTypes: FIXTURE_TYPES, subagentModels: FIXTURE_SUBAGENT_MODELS });
  process.env.OPENCLAW_MODEL_TIERING = '1';
});

test.afterEach(() => {
  _setConfigForTests({ subagentTypes: null, subagentModels: null });
  delete process.env.OPENCLAW_MODEL_TIERING;
});

test('node_model → tier → model id (full resolution chain)', () => {
  assert.equal(
    resolveModel('mastermind', 'comprehensive-review', 'memo_writer'),
    'claude-opus-4-7'
  );
});

test('node_model can be a literal model id (no tier lookup)', () => {
  assert.equal(
    resolveModel('mastermind', 'critique', 'literal_model'),
    'claude-haiku-4-5-20251001'
  );
});

test('unknown node_model falls back to debator tier with a warn', () => {
  // No "unknown_node" defined for critique mode → falls back to debator
  assert.equal(
    resolveModel('mastermind', 'critique', 'unknown_node'),
    'claude-sonnet-4-6'
  );
});

test('unknown tier in node_models falls through to SUBAGENT_MODELS default', () => {
  const t = JSON.parse(JSON.stringify(FIXTURE_TYPES));
  t.types.mastermind.model_modes.critique.node_models.broken = 'nonexistent_tier';
  _setConfigForTests({ subagentTypes: t, subagentModels: FIXTURE_SUBAGENT_MODELS });
  assert.equal(
    resolveModel('mastermind', 'critique', 'broken'),
    'claude-opus-4-7-default'
  );
});

test('subagent with no tier config returns SUBAGENT_MODELS default', () => {
  assert.equal(
    resolveModel('paperhunter', 'any-mode', 'any-node'),
    'claude-sonnet-4-6'
  );
});

test('OPENCLAW_MODEL_TIERING off → always returns SUBAGENT_MODELS default', () => {
  delete process.env.OPENCLAW_MODEL_TIERING;
  // Even though node_models would resolve to opus, the gate blocks it
  assert.equal(
    resolveModel('mastermind', 'comprehensive-review', 'memo_writer'),
    'claude-opus-4-7-default'
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/test_resolve_model.test.js`
Expected: FAIL with `Cannot find module '.../src/agent/config/resolve_model.js'`

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_resolve_model.test.js
git commit -m "test(model-tiering): add failing tests for resolveModel"
```

---

## Task 2: Implement resolveModel()

**Files:**
- Create: `src/agent/config/resolve_model.js`

- [ ] **Step 1: Write the implementation**

```javascript
'use strict';

/**
 * resolveModel(subagentType, mode, nodeName) → modelId
 *
 * Resolution order:
 *   subagent.model_modes[mode].node_models[nodeName]
 *     → could be a tier name OR a literal model id
 *     → tier name resolves via subagent.model_tiers
 *   fallback: SUBAGENT_MODELS[subagentType].model
 *
 * Gated by env OPENCLAW_MODEL_TIERING=1; off → always returns the fallback.
 */

const path = require('node:path');
const fs   = require('node:fs');

let _subagentTypes  = null;
let _subagentModels = null;

function _loadDefaults() {
  if (_subagentTypes === null) {
    const ROOT = path.resolve(__dirname, '..', '..', '..');
    _subagentTypes = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/agent/config/subagent-types.json'), 'utf8')
    );
  }
  if (_subagentModels === null) {
    const { SUBAGENT_MODELS } = require('./models.js');
    _subagentModels = SUBAGENT_MODELS;
  }
}

function _setConfigForTests({ subagentTypes, subagentModels }) {
  _subagentTypes  = subagentTypes  === null ? null : subagentTypes;
  _subagentModels = subagentModels === null ? null : subagentModels;
}

function _defaultModel(subagentType) {
  _loadDefaults();
  const entry = _subagentModels?.[subagentType];
  // Pull from either {model: '...'} or a literal string
  if (entry && typeof entry === 'object' && entry.model) return entry.model;
  if (typeof entry === 'string') return entry;
  return null;
}

function resolveModel(subagentType, mode, nodeName) {
  // Gate
  if (process.env.OPENCLAW_MODEL_TIERING !== '1') {
    return _defaultModel(subagentType);
  }

  _loadDefaults();
  const type = _subagentTypes?.types?.[subagentType];
  if (!type) return _defaultModel(subagentType);

  const modeConfig = type.model_modes?.[mode];
  const nodeValue  = modeConfig?.node_models?.[nodeName];

  if (nodeValue) {
    // First try as a tier name
    const tierModel = type.model_tiers?.[nodeValue];
    if (tierModel) return tierModel;
    // If it looks like a model id (contains 'claude-' or has a dash + version),
    // accept as literal
    if (typeof nodeValue === 'string' && nodeValue.includes('claude-')) {
      return nodeValue;
    }
    // Unknown tier name and not a literal model id → warn and fall through
    console.warn(
      `[resolve_model] subagent=${subagentType} mode=${mode} node=${nodeName}: ` +
      `value ${JSON.stringify(nodeValue)} is not a known tier and not a model id; ` +
      `falling back to default.`
    );
    return _defaultModel(subagentType);
  }

  // No node_models entry — try debator tier as a soft default for this mode
  if (modeConfig && type.model_tiers?.debator) {
    return type.model_tiers.debator;
  }

  return _defaultModel(subagentType);
}

module.exports = { resolveModel, _setConfigForTests };
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `node --test tests/test_resolve_model.test.js`
Expected: PASS — 6 tests pass.

- [ ] **Step 3: Commit the implementation**

```bash
git add src/agent/config/resolve_model.js
git commit -m "feat(model-tiering): add resolveModel helper"
```

---

## Task 3: Add `model_tiers` + `model_modes` to mastermind in subagent-types.json

**Files:**
- Modify: `src/agent/config/subagent-types.json`

- [ ] **Step 1: Find the existing `mastermind` block**

The mastermind entry currently looks like:

```jsonc
"mastermind": {
  "promptFile": "src/agent/prompts/subagents/mastermind.md",
  // ... existing fields ...
  "modes": ["RESEARCH", "STRATEGY_STACK"],
  // ... description ...
}
```

- [ ] **Step 2: Add `model_tiers` and `model_modes` (new fields)**

Insert two new sibling fields ALONGSIDE existing `modes`. `modes` (array) is a deployment-gate concept; `model_modes` (object) is new for B3 — distinct names to avoid collision.

```jsonc
"mastermind": {
  "promptFile": "src/agent/prompts/subagents/mastermind.md",
  // ... existing fields unchanged ...
  "modes": ["RESEARCH", "STRATEGY_STACK"],
  "model_tiers": {
    "judge":       "claude-opus-4-7",
    "synthesizer": "claude-opus-4-7",
    "debator":     "claude-sonnet-4-6",
    "extractor":   "claude-sonnet-4-6"
  },
  "model_modes": {
    "comprehensive-review": {
      "node_models": { "memo_writer": "judge" }
    }
  },
  // ... rest unchanged ...
}
```

(The `critique` and `synthesize` modes will be added by Plan F3. We only add what proves the helper here.)

- [ ] **Step 3: Verify JSON is still valid**

Run: `python3 -c "import json; json.load(open('src/agent/config/subagent-types.json')); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/agent/config/subagent-types.json
git commit -m "feat(model-tiering): add model_tiers + model_modes to mastermind"
```

---

## Task 4: Add OPENCLAW_MODEL_TIERING constant to models.js

**Files:**
- Modify: `src/agent/config/models.js`

- [ ] **Step 1: Write a failing test for the env constant**

Append to `tests/test_resolve_model.test.js`:

```javascript
test('OPENCLAW_MODEL_TIERING constant is exported from models.js', () => {
  const { TIERING_ENV } = require(path.join(ROOT, 'src/agent/config/models.js'));
  assert.equal(TIERING_ENV, 'OPENCLAW_MODEL_TIERING');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/test_resolve_model.test.js`
Expected: FAIL on the new test only (`TIERING_ENV` is undefined).

- [ ] **Step 3: Add the export**

Open `src/agent/config/models.js`. After the existing `module.exports` block, add `TIERING_ENV` to the export object:

```javascript
// Existing export — keep it, just add TIERING_ENV
module.exports = {
  MODELS,
  SUBAGENT_MODELS,
  FLASH_MODEL,
  CONTEXT_LIMITS,
  TIERING_ENV: 'OPENCLAW_MODEL_TIERING',  // <-- add this line
};
```

(If the file uses individual `module.exports.X = ...` lines, add `module.exports.TIERING_ENV = 'OPENCLAW_MODEL_TIERING';` at the bottom instead.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/test_resolve_model.test.js`
Expected: PASS — all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent/config/models.js tests/test_resolve_model.test.js
git commit -m "feat(model-tiering): export TIERING_ENV constant"
```

---

## Task 5: Wire `resolveModel` into comprehensive_review.js (proof-of-life)

**Files:**
- Modify: `src/agent/curators/comprehensive_review.js`
- Test: `tests/test_comprehensive_review_uses_tiering.test.js` (new)

- [ ] **Step 1: Locate the existing model selection**

Run:

```bash
grep -n -E "MODELS\.|opus|sonnet|primary|orchestrator" src/agent/curators/comprehensive_review.js | head -20
```

You should see one or two lines like `const model = MODELS.opus1m.model` or similar. Note the exact line numbers.

- [ ] **Step 2: Write a failing integration test**

Create `tests/test_comprehensive_review_uses_tiering.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');

test('comprehensive_review imports resolveModel', () => {
  const src = require('node:fs').readFileSync(
    path.join(ROOT, 'src/agent/curators/comprehensive_review.js'),
    'utf8'
  );
  assert.ok(
    src.includes("require('../config/resolve_model')") ||
    src.includes('require("../config/resolve_model")'),
    'comprehensive_review.js should require resolve_model'
  );
  assert.ok(
    src.includes("resolveModel('mastermind', 'comprehensive-review', 'memo_writer')"),
    'comprehensive_review.js should call resolveModel with the memo_writer node name'
  );
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `node --test tests/test_comprehensive_review_uses_tiering.test.js`
Expected: FAIL — `comprehensive_review.js should require resolve_model`.

- [ ] **Step 4: Wire `resolveModel` into the existing model-selection line**

Open `src/agent/curators/comprehensive_review.js`. Near the top, add the require:

```javascript
const { resolveModel } = require('../config/resolve_model');
```

Find the existing model-selection line you noted in Step 1. Replace it. Example transform:

```javascript
// BEFORE:
const model = MODELS.opus1m.model;

// AFTER:
const model = resolveModel('mastermind', 'comprehensive-review', 'memo_writer');
```

(Keep any surrounding code unchanged. If the curator uses the model in multiple places — e.g. one call for memos, another for some other inner step — only wire the memo-writing call. Other internal calls stay on the old default for now; B3 only proves the substrate works.)

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
node --test tests/test_resolve_model.test.js tests/test_comprehensive_review_uses_tiering.test.js
```

Expected: PASS — all tests pass.

- [ ] **Step 6: Smoke test — dry-run comprehensive-review**

Run:

```bash
OPENCLAW_MODEL_TIERING=1 node src/agent/curators/run_mastermind.js --mode comprehensive-review --dry-run 2>&1 | head -40
```

Look in the output for a line referencing `resolveModel` or the resolved model id (`claude-opus-4-7`). The run may exit with "no strategies" or "no memos" in dry-run; that's fine. We're only verifying the helper is wired without crashing.

- [ ] **Step 7: Commit**

```bash
git add src/agent/curators/comprehensive_review.js tests/test_comprehensive_review_uses_tiering.test.js
git commit -m "feat(model-tiering): wire resolveModel into comprehensive_review (memo_writer node)"
```

---

## Done

After Task 5, B3 ships. Subagents continue using their `SUBAGENT_MODELS` default unless the env flag is set AND they declare `model_tiers` / `model_modes`. F3 will lean on this substrate.

**Operator enablement step (manual, not part of TDD):** flip `OPENCLAW_MODEL_TIERING=1` in `.env`, restart `johnbot.service`, observe one Saturday `comprehensive-review` cycle — check journalctl for the resolveModel log line and confirm the memo writer uses `claude-opus-4-7`. After 1 successful cycle, this becomes the new baseline.
