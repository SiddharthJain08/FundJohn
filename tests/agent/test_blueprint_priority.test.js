const assert = require('assert');
const { partitionBlueprintBudget } = require('../../src/agent/curators/saturday_brain.js');

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

// Finding 1 (review): an all-blueprint backlog (the post-bulk-import state) must
// use the FULL cap, not just the reserved share — blueprint absorbs paper's
// unused slots symmetrically. Without the second pass this drained at half rate.
const allBp = [
  { candidate_id: 'g1', origin: 'git_blueprint' },
  { candidate_id: 'g2', origin: 'git_blueprint' },
  { candidate_id: 'g3', origin: 'git_blueprint' },
  { candidate_id: 'g4', origin: 'git_blueprint' },
];
const r2 = partitionBlueprintBudget(allBp, /*cap*/ 4, /*blueprintShare*/ 0.5);
assert.strictEqual(r2.blueprintCap, 4); // full cap, not ceil(4*0.5)=2
assert.strictEqual(r2.paperCap, 0);

// Mixed pool where papers under-fill: blueprint should absorb the slack too.
const mixed = [
  { candidate_id: 'g1', origin: 'git_blueprint' },
  { candidate_id: 'g2', origin: 'git_blueprint' },
  { candidate_id: 'g3', origin: 'git_blueprint' },
  { candidate_id: 'p1', origin: 'paper' },
];
const r3 = partitionBlueprintBudget(mixed, /*cap*/ 4, /*blueprintShare*/ 0.5);
assert.strictEqual(r3.blueprintCap, 3); // min(3 available, cap-paperCap=4-1)
assert.strictEqual(r3.paperCap, 1);

console.log('ok');
