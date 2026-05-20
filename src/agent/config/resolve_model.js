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
