'use strict';
const assert = require('assert');
const { terminalStatusFor } = require('../src/agent/curators/_candidate_terminal_status');

// Tier-A coded+promoted -> done
assert.strictEqual(terminalStatusFor({ tier: 'A', promoted: true }), 'done');
// Tier-A attempted but not promoted (coding failed) -> null (leave pending for retry, do NOT mislabel terminal)
assert.strictEqual(terminalStatusFor({ tier: 'A', promoted: false }), null);
// Tier-B staged -> blocked_buildable
assert.strictEqual(terminalStatusFor({ tier: 'B', promoted: false }), 'blocked_buildable');
// Tier-C no-provider (the common case) -> blocked_unclassified
assert.strictEqual(terminalStatusFor({ tier: 'C', promoted: false }), 'blocked_unclassified');
assert.strictEqual(terminalStatusFor({ tier: 'C' }), 'blocked_unclassified');
// rejected takes precedence over tier (mirrors backfill CASE precedence)
assert.strictEqual(terminalStatusFor({ tier: 'C', rejected: true }), 'blocked_rejected');
assert.strictEqual(terminalStatusFor({ tier: 'B', rejected: true }), 'blocked_rejected');
// unknown tier -> null
assert.strictEqual(terminalStatusFor({ tier: 'Z', promoted: false }), null);
console.log('ok test_candidate_terminal_status');
