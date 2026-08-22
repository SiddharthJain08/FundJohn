'use strict';
// Multi-provider model config
const MODELS = {
  orchestrator: {
    provider: 'anthropic',
    model: 'claude-sonnet-5',
    description: 'BotJohn — orchestrator, portfolio manager',
  },
  primary: {
    provider: 'anthropic',
    model: 'claude-sonnet-5',
    description: 'Default — TradeJohn',
  },
  fast: {
    provider: 'anthropic',
    model: 'claude-haiku-4-5-20251001',
    description: 'Fast/cheap model for lightweight tasks.',
  },
  opus1m: {
    provider: 'anthropic',
    model: 'claude-opus-5[1m]',
    description: 'Opus 5 (1M context) — corpus curation at scale. (Opus 4.7[1m] until 2026-08-22.)',
  },
  failover: {
    provider: 'anthropic',
    model: 'claude-sonnet-5',
    description: 'Failover if primary unavailable',
  },
};

// Subagent model assignments — FundJohn system (ResearchJohn retired 2026-05-02)
const SUBAGENT_MODELS = {
  botjohn:          MODELS.orchestrator,
  tradejohn:        MODELS.primary,
  paperhunter:      MODELS.primary,
  strategycoder:    MODELS.primary,
  'mastermind':     MODELS.opus1m,
  'corpus-curator': MODELS.opus1m,
};

// Flash model alias for quick lookups
const FLASH_MODEL = MODELS.fast;

// Context window limits per model
// Claude 5 family (2026-08-22 upgrade): bare ids are treated as 200k for
// compaction purposes (conservative — claude-bin's `[1m]` suffix is what
// explicitly opts a session into the 1M window); the `[1m]` variants are 1M.
const CONTEXT_LIMITS = {
  'claude-fable-5':            200_000,
  'claude-fable-5[1m]':        1_000_000,
  'claude-opus-5':             200_000,
  'claude-opus-5[1m]':         1_000_000,  // Mastermind / corpus-curator since 2026-08-22
  'claude-sonnet-5':           200_000,
  'claude-sonnet-5[1m]':       1_000_000,
  'claude-opus-4-6':           200_000,
  'claude-opus-4-7':           200_000,
  'claude-opus-4-7[1m]':       1_000_000,  // 1M context variant — used by Mastermind
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

module.exports = { MODELS, SUBAGENT_MODELS, FLASH_MODEL, COMPACTION, getModelForSubagent, getContextLimit, TIERING_ENV: 'OPENCLAW_MODEL_TIERING' };
