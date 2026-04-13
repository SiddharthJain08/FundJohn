'use strict';

// Patterns to redact from tool results
const REDACTION_PATTERNS = [
  // API keys injected via env
  { env: 'FMP_API_KEY',            label: '[FMP_KEY_REDACTED]' },
  { env: 'ALPHA_VANTAGE_API_KEY',  label: '[AV_KEY_REDACTED]' },
  { env: 'POLYGON_API_KEY',        label: '[POLYGON_KEY_REDACTED]' },
  { env: 'TAVILY_API_KEY',         label: '[TAVILY_KEY_REDACTED]' },
  { env: 'ANTHROPIC_API_KEY',      label: '[ANTHROPIC_KEY_REDACTED]' },
  { env: 'DISCORD_BOT_TOKEN',      label: '[DISCORD_TOKEN_REDACTED]' },
  { env: 'POSTGRES_URI',           label: '[POSTGRES_URI_REDACTED]' },
  { env: 'REDIS_URL',              label: '[REDIS_URL_REDACTED]' },
];

// Regex patterns for common secret shapes
const SHAPE_PATTERNS = [
  { regex: /apikey=[A-Za-z0-9]{20,}/g, label: 'apikey=[KEY_REDACTED]' },
  { regex: /api_key=[A-Za-z0-9]{20,}/g, label: 'api_key=[KEY_REDACTED]' },
  { regex: /token=[A-Za-z0-9._-]{30,}/g, label: 'token=[TOKEN_REDACTED]' },
  { regex: /Bearer [A-Za-z0-9._-]{30,}/g, label: 'Bearer [TOKEN_REDACTED]' },
];

function redactString(text) {
  if (typeof text !== 'string') return text;

  let redacted = text;

  // Redact known env var values
  for (const { env, label } of REDACTION_PATTERNS) {
    const value = process.env[env];
    if (value && value.length > 8) {
      redacted = redacted.split(value).join(label);
    }
  }

  // Redact by shape
  for (const { regex, label } of SHAPE_PATTERNS) {
    redacted = redacted.replace(regex, label);
  }

  return redacted;
}

function redactMessage(msg) {
  if (!msg) return msg;
  const content = msg.content;
  if (typeof content === 'string') {
    return { ...msg, content: redactString(content) };
  }
  if (Array.isArray(content)) {
    return { ...msg, content: content.map((block) => {
      if (block.type === 'text') return { ...block, text: redactString(block.text) };
      if (block.type === 'tool_result') return { ...block, content: redactString(JSON.stringify(block.content)) };
      return block;
    })};
  }
  return msg;
}

/**
 * Secret redaction middleware — scrubs API keys and tokens from all tool results.
 * Runs before any other middleware that reads tool results.
 */
async function secretRedaction(state, next) {
  const { messages } = state;
  const redacted = messages.map((msg) => {
    if (msg.role === 'tool' || msg.role === 'user') return redactMessage(msg);
    return msg;
  });
  return next({ ...state, messages: redacted });
}

module.exports = secretRedaction;
