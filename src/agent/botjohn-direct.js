'use strict';

const fs   = require('fs');
const path = require('path');
const Anthropic    = require('@anthropic-ai/sdk');
const chatHistory  = require('./chat-history');
const { buildSystemContext } = require('./system-context');

const SYSTEM_PROMPT = fs.readFileSync(
  path.join(__dirname, 'prompts/subagents/botjohn.md'),
  'utf8'
);

async function respond({ participantId, participantName, participantType, channelId, message }) {
  const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  const [history, systemCtx] = await Promise.all([
    chatHistory.loadHistory(participantId),
    buildSystemContext(),
  ]);

  const systemBlock = systemCtx
    ? `${SYSTEM_PROMPT}\n\n${systemCtx}`
    : SYSTEM_PROMPT;

  const response = await client.messages.create({
    model:      'claude-sonnet-4-6',
    max_tokens: 4096,
    system:     systemBlock,
    messages: [
      ...history,
      { role: 'user', content: `[${participantName}]: ${message}` },
    ],
  });

  const text = response.content.find(b => b.type === 'text')?.text ?? '';

  await chatHistory.saveExchange(
    participantId, participantName, participantType, channelId, message, text
  );

  return { output: text, usage: response.usage };
}

module.exports = { respond };
