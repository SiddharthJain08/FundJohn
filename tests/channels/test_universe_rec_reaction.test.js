'use strict';

/**
 * tests/test_universe_rec_reaction.test.js
 *
 * Unit tests for the pure parseUniverseRecReaction helper.
 * No Discord client, no DB, no network.
 *
 * Run:
 *   node --test tests/test_universe_rec_reaction.test.js
 */

const { test }  = require('node:test');
const assert    = require('node:assert/strict');

const { parseUniverseRecReaction } = require('../../src/channels/discord/_universe_rec_reaction');

// ── Footer in message content → extracts recId ───────────────────────────────

test('approve emoji + footer in content → { recId, action: approve }', () => {
  const content = [
    '## Universe Rec for momentum_12_1',
    'Mastermind recommends switching to no_adr predicate.',
    '_footer: universe-rec:42_',
  ].join('\n');
  const result = parseUniverseRecReaction(content, '✅');
  assert.deepStrictEqual(result, { recId: '42', action: 'approve' });
});

test('reject emoji + footer in content → { recId, action: reject }', () => {
  const content = 'Strategy rec blurb\n_footer: universe-rec:99_';
  const result = parseUniverseRecReaction(content, '❌');
  assert.deepStrictEqual(result, { recId: '99', action: 'reject' });
});

test('defer emoji + footer in content → { recId, action: defer }', () => {
  const content = 'Some rec text\n_footer: universe-rec:7_';
  const result = parseUniverseRecReaction(content, '⏸');
  assert.deepStrictEqual(result, { recId: '7', action: 'defer' });
});

test('recId can be multi-digit', () => {
  const content = '_footer: universe-rec:12345_';
  const result = parseUniverseRecReaction(content, '✅');
  assert.ok(result !== null, 'should parse');
  assert.strictEqual(result.recId, '12345');
  assert.strictEqual(result.action, 'approve');
});

// ── Non-matching content → null ───────────────────────────────────────────────

test('message with no universe-rec footer → null', () => {
  const content = 'This is a random Discord message with no rec footer.';
  const result = parseUniverseRecReaction(content, '✅');
  assert.strictEqual(result, null);
});

test('empty content → null', () => {
  const result = parseUniverseRecReaction('', '✅');
  assert.strictEqual(result, null);
});

test('null content → null (defensive)', () => {
  const result = parseUniverseRecReaction(null, '✅');
  assert.strictEqual(result, null);
});

// ── Unknown emoji → null ──────────────────────────────────────────────────────

test('unknown emoji on a valid rec message → null', () => {
  const content = 'Good rec!\n_footer: universe-rec:5_';
  const result = parseUniverseRecReaction(content, '👍');
  assert.strictEqual(result, null);
});

test('empty emoji string → null', () => {
  const content = '_footer: universe-rec:5_';
  const result = parseUniverseRecReaction(content, '');
  assert.strictEqual(result, null);
});

// ── Emoji-to-action mapping completeness ─────────────────────────────────────

test('✅ maps to approve', () => {
  const r = parseUniverseRecReaction('_footer: universe-rec:1_', '✅');
  assert.strictEqual(r?.action, 'approve');
});

test('❌ maps to reject', () => {
  const r = parseUniverseRecReaction('_footer: universe-rec:1_', '❌');
  assert.strictEqual(r?.action, 'reject');
});

test('⏸ maps to defer', () => {
  const r = parseUniverseRecReaction('_footer: universe-rec:1_', '⏸');
  assert.strictEqual(r?.action, 'defer');
});

// ── Regex handles footer inline / mid-message ─────────────────────────────────

test('footer anywhere in content is found', () => {
  const content = 'Line1\nLine2\n_footer: universe-rec:88_\nLine4';
  const result = parseUniverseRecReaction(content, '✅');
  assert.deepStrictEqual(result, { recId: '88', action: 'approve' });
});

// ── Consistent with _formatDiscordMessage footer format ───────────────────────

test('matches format produced by _formatDiscordMessage (regression)', () => {
  // The curator posts: _footer: universe-rec:<recId>_
  // This verifies the regex /universe-rec:(\d+)/ captures the ID correctly.
  const recId = 123;
  const content = `Some detailed recommendation text here.\n_footer: universe-rec:${recId}_`;
  const result = parseUniverseRecReaction(content, '✅');
  assert.ok(result !== null, 'should parse formatted footer');
  assert.strictEqual(Number(result.recId), recId);
  assert.strictEqual(result.action, 'approve');
});
