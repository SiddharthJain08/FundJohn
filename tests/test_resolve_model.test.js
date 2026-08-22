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

test('OPENCLAW_MODEL_TIERING constant is exported from models.js', () => {
  const { TIERING_ENV } = require(path.join(ROOT, 'src/agent/config/models.js'));
  assert.equal(TIERING_ENV, 'OPENCLAW_MODEL_TIERING');
});

test('flag-off resolves through real SUBAGENT_MODELS — mastermind gets 1M Opus', () => {
  delete process.env.OPENCLAW_MODEL_TIERING;
  // Use real config (not fixtures)
  _setConfigForTests({ subagentTypes: null, subagentModels: null });
  assert.equal(
    resolveModel('mastermind', 'comprehensive-review', 'memo_writer'),
    'claude-opus-5[1m]'
  );
});

test('unknown subagentType falls back to MODELS.primary.model with a warn', () => {
  // Gate state irrelevant — _defaultModel runs in both branches
  delete process.env.OPENCLAW_MODEL_TIERING;
  _setConfigForTests({ subagentTypes: null, subagentModels: null });
  const result = resolveModel('mastermimd-typo', 'any-mode', 'any-node');
  assert.ok(
    typeof result === 'string' && result.length > 0,
    `should return a non-null model string; got ${JSON.stringify(result)}`
  );
  // Real MODELS.primary.model is currently 'claude-sonnet-4-6' (verify it's a claude model)
  assert.match(result, /^claude-/, `expected a claude-* model; got ${result}`);
});

test('unknown subagentType warns once per subagent, not every call', () => {
  const { _resetWarnedForTests } = require(path.join(ROOT, 'src/agent/config/resolve_model.js'));
  _resetWarnedForTests();
  _setConfigForTests({ subagentTypes: null, subagentModels: null });

  // Capture stderr warns
  const origWarn = console.warn;
  const warns = [];
  console.warn = (...args) => warns.push(args.join(' '));

  try {
    resolveModel('mystery-bot', 'x', 'y');
    resolveModel('mystery-bot', 'x', 'y');  // same unknown — should NOT warn again
    resolveModel('mystery-bot', 'x', 'y');
    assert.equal(warns.length, 1, `expected 1 warn, got ${warns.length}`);

    resolveModel('other-mystery', 'x', 'y');  // different unknown — SHOULD warn
    assert.equal(warns.length, 2);
  } finally {
    console.warn = origWarn;
    _resetWarnedForTests();
  }
});

test('getContextLimit returns 1M for claude-opus-4-7[1m]', () => {
  const { getContextLimit } = require(path.join(ROOT, 'src/agent/config/models.js'));
  assert.equal(getContextLimit('claude-opus-4-7[1m]'), 1_000_000);
  // And the bare form is now 200K (not 1M)
  assert.equal(getContextLimit('claude-opus-4-7'), 200_000);
});
