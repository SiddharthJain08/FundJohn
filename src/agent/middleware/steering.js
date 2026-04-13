'use strict';

const { drainSteering } = require('../../database/redis');

/**
 * Steering middleware — drains Redis steering queue before each LLM call.
 * Injected messages appear as user messages in conversation state.
 * Enables !john follow-up commands while agent is mid-run.
 */
async function steering(state, next) {
  const { threadId, messages } = state;
  if (!threadId) return next(state);

  const steeringMessages = await drainSteering(threadId);
  if (steeringMessages.length > 0) {
    console.log(`[steering] Injecting ${steeringMessages.length} steering messages for thread ${threadId}`);
    const injected = steeringMessages.map((msg) => ({
      role: 'user',
      content: msg,
    }));
    return next({
      ...state,
      messages: [...messages, ...injected],
    });
  }

  return next(state);
}

module.exports = steering;
