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
