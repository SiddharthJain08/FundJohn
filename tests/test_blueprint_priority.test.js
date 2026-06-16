const assert = require('assert');
const { partitionBlueprintBudget, promotionThresholdFor } = require('../src/agent/curators/saturday_brain.js');

const cands = [
  { candidate_id: 'p1', origin: 'paper' },
  { candidate_id: 'g1', origin: 'git_blueprint' },
  { candidate_id: 'b1', origin: 'blog_blueprint' },
  { candidate_id: 'p2', origin: 'paper' },
];
const r = partitionBlueprintBudget(cands, /*cap*/ 3, /*blueprintShare*/ 0.5);
assert.deepStrictEqual(r.ordered.map(c => c.candidate_id), ['g1','b1','p1','p2']); // blueprint first
assert.strictEqual(r.blueprintCap, 2);
assert.strictEqual(r.paperCap, 1);
assert.ok(promotionThresholdFor('git_blueprint').min_sharpe < promotionThresholdFor('paper').min_sharpe);
assert.strictEqual(promotionThresholdFor('paper').min_sharpe, 0.5);
console.log('ok');
