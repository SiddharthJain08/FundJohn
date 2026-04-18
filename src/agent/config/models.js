'use strict';
// Multi-provider model config
const MODELS = {
  orchestrator: {
    provider: 'anthropic',
    model: 'claude-opus-4-6',
    description: 'BotJohn — orchestrator, portfolio manager, full reasoning depth',
  },
  primary: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-6',
    description: 'Default — ResearchJohn, TradeJohn',
  },
  fast: {
    provider: 'anthropic',
    model: 'claude-haiku-4-5-20251001',
    description: 'Fast/cheap model for lightweight tasks.',
  },
  failover: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-6',
    description: 'Failover if primary unavailable',
  },
};

// Subagent model assignments — 4-agent FundJohn system
const SUBAGENT_MODELS = {
  botjohn:       MODELS.orchestrator,
  researchjohn:  MODELS.primary,
  tradejohn:     MODELS.primary,
paperhunter:   MODELS.fast,
  strategycoder: MODELS.primary,
};

// Flash model alias for quick lookups
const FLASH_MODEL = MODELS.fast;

// Context window limits per model
const CONTEXT_LIMITS = {
  'claude-opus-4-6':           200_000,
  'claude-sonnet-4-6':         200_000,
  'claude-haiku-4-5-20251001': 200_000,
};

// Compaction thresholds (fraction of context window)
const COMPACTION = {
  tier1_truncate:  0.60,  // 60% → truncate large tool results
  tier2_summarize: 0.85,  // 85% → evict old messages + LLM summarize
};

function getModelForSubagent(type) {
  return SUBAGENT_MODELS[type] || MODELS.primary;
}

function getContextLimit(modelId) {
  return CONTEXT_LIMITS[modelId] || 200_000;
}

module.exports = { MODELS, SUBAGENT_MODELS, FLASH_MODEL, COMPACTION, getModelForSubagent, getContextLimit };
