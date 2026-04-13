'use strict';

// Multi-provider model config with failover
const MODELS = {
  primary: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-6',
    description: 'Default — complex tasks, full diligence, trade pipelines',
  },
  fast: {
    provider: 'anthropic',
    model: 'claude-haiku-4-5-20251001',
    description: 'Flash mode — quick lookups, status, simple queries',
  },
  failover: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-6',
    description: 'Failover if primary unavailable',
  },
};

// Subagent model assignments
const SUBAGENT_MODELS = {
  research:        MODELS.primary,
  'data-prep':     MODELS.primary,
  'equity-analyst': MODELS.primary,
  'report-builder': MODELS.primary,
  compute:         MODELS.primary,
};

// Flash mode uses fast model
const FLASH_MODEL = MODELS.fast;

// Context window limits per model
const CONTEXT_LIMITS = {
  'claude-sonnet-4-6':        200_000,
  'claude-haiku-4-5-20251001': 200_000,
};

// Compaction thresholds (fraction of context window)
const COMPACTION = {
  tier1_truncate: 0.60,  // 60% → truncate large tool results
  tier2_summarize: 0.85, // 85% → evict old messages + LLM summarize
};

function getModelForSubagent(type) {
  return SUBAGENT_MODELS[type] || MODELS.primary;
}

function getContextLimit(modelId) {
  return CONTEXT_LIMITS[modelId] || 200_000;
}

module.exports = { MODELS, SUBAGENT_MODELS, FLASH_MODEL, COMPACTION, getModelForSubagent, getContextLimit };
