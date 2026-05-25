'use strict';

/**
 * _universe_rec_reaction.js
 *
 * Pure, side-effect-free helpers for the Discord universe-rec reaction handler.
 * Extracted so they can be unit-tested without importing the full bot.js.
 *
 * Usage in bot.js:
 *   const { parseUniverseRecReaction } = require('./_universe_rec_reaction');
 */

const EMOJI_MAP = {
  '✅': 'approve',
  '❌': 'reject',
  '⏸': 'defer',
};

const REC_ID_RE = /universe-rec:(\d+)/;

/**
 * Parse a Discord reaction on a universe-rec message.
 *
 * @param {string} messageContent - The full text content of the reacted-to message.
 * @param {string} emojiName      - reaction.emoji.name (the Unicode character).
 * @returns {{ recId: string, action: string } | null}
 *   null if the message is not a universe-rec, or if the emoji is not mapped.
 */
function parseUniverseRecReaction(messageContent, emojiName) {
  const match = REC_ID_RE.exec(messageContent || '');
  if (!match) return null;

  const action = EMOJI_MAP[emojiName];
  if (!action) return null;

  return { recId: match[1], action };
}

module.exports = { parseUniverseRecReaction, EMOJI_MAP, REC_ID_RE };
