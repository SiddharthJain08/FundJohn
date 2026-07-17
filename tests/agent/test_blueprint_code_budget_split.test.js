'use strict';
// Part 4 (Blueprint Fast Lane Phase 1.2 activation): the Tier-A coding loop in
// saturday_brain._code and saturday_brain_finisher must (a) attempt
// blueprint-origin candidates FIRST and (b) honor the per-origin budget split
// from partitionBlueprintBudget, deferring (NOT failing) candidates whose origin
// slice is exhausted, with total attempts <= cap.
//
// _code / the finisher main() are not unit-testable without DB + ResearchOrchestrator
// mocking, so this test exercises the exact two-counter walk those loops use,
// driven by the SAME partitionBlueprintBudget the live code imports. It locks in
// the accounting invariants (blueprint-first, budget split, deferred!=failed).
const assert = require('assert');
const { partitionBlueprintBudget, BLUEPRINT_ORIGINS } =
  require('../../src/agent/curators/saturday_brain.js');

// Mirror the live loop (saturday_brain_finisher.js Phase 6 / saturday_brain.js _code):
// a stubbed coder that "promotes" everything it attempts; record attempt order.
function runCodeLoop(tierA, cap, codeStub) {
  const { ordered, blueprintCap, paperCap } = partitionBlueprintBudget(tierA, cap);
  let coded = 0, failed = 0, bpUsed = 0, pUsed = 0, attempted = 0;
  const attemptOrder = [];
  for (const el of ordered) {
    const isBp = BLUEPRINT_ORIGINS.has(el.origin);
    if (isBp ? (bpUsed >= blueprintCap) : (pUsed >= paperCap)) continue;  // defer
    if (isBp) bpUsed++; else pUsed++;
    attempted++;
    const sid = el.hunterResult.strategy_id || null;
    if (!sid) { failed++; continue; }                                     // attempt that fails
    attemptOrder.push({ id: el.candidateId, origin: el.origin });
    if (codeStub(el)) coded++; else failed++;
  }
  return { coded, failed, attempted, attemptOrder, blueprintCap, paperCap };
}

const mk = (id, origin, sid = id) =>
  ({ candidateId: id, origin, hunterResult: { strategy_id: sid } });

// ── Mixed pool: 3 blueprint + 3 paper, cap 4, 50% share → bp=2, paper=2 ─────
const tierA = [
  mk('p1', 'paper'),
  mk('g1', 'git_blueprint'),
  mk('p2', 'paper'),
  mk('b1', 'blog_blueprint'),
  mk('p3', 'paper'),
  mk('g2', 'git_blueprint'),
];
const r = runCodeLoop(tierA, /*cap*/ 4, () => true);

// Budget split: ceil(4*0.5)=2 blueprint reserved; paper gets the rest (2).
assert.strictEqual(r.blueprintCap, 2, 'blueprintCap = ceil(cap*share)');
assert.strictEqual(r.paperCap, 2, 'paperCap = cap - blueprintCap');
// Total attempts must not exceed cap.
assert.strictEqual(r.attempted, 4, 'attempts == cap');
assert.strictEqual(r.coded, 4);
assert.strictEqual(r.failed, 0);
// Blueprint candidates attempted FIRST (the two blueprint slots before any paper).
assert.deepStrictEqual(
  r.attemptOrder.map(a => a.origin),
  ['git_blueprint', 'blog_blueprint', 'paper', 'paper'],
  'blueprint-origin candidates coded before paper');
// Exactly the 2 reserved blueprint ids (in ordered order g1,b1) + first 2 papers.
assert.deepStrictEqual(r.attemptOrder.map(a => a.id), ['g1', 'b1', 'p1', 'p2']);

// ── Deferred != failed: blueprint-heavy pool, small cap ─────────────────────
// 4 blueprint + 1 paper, cap 2 → bp reserved=1, paper=1. The 3 un-slotted
// blueprints + 0 papers are DEFERRED (not counted as failures).
const heavy = [
  mk('g1', 'git_blueprint'), mk('g2', 'git_blueprint'),
  mk('g3', 'git_blueprint'), mk('g4', 'git_blueprint'),
  mk('p1', 'paper'),
];
const r2 = runCodeLoop(heavy, /*cap*/ 2, () => true);
assert.strictEqual(r2.blueprintCap, 1, 'ceil(2*0.5)=1 blueprint slot');
assert.strictEqual(r2.paperCap, 1);
assert.strictEqual(r2.attempted, 2, 'attempts capped at 2, rest deferred');
assert.strictEqual(r2.failed, 0, 'deferred candidates are NOT failures');
assert.deepStrictEqual(r2.attemptOrder.map(a => a.id), ['g1', 'p1']);

// ── Missing-sid still consumes a slot and counts as failed (orig i<cap parity) ─
const withBad = [
  mk('g1', 'git_blueprint', null),  // no strategy_id
  mk('g2', 'git_blueprint'),
  mk('p1', 'paper'),
];
const r3 = runCodeLoop(withBad, /*cap*/ 2, () => true);
// cap 2 → bp=1, paper=1. g1 (bad sid) consumes the single blueprint slot and
// fails; g2 is deferred (slot full); p1 codes.
assert.strictEqual(r3.attempted, 2);
assert.strictEqual(r3.failed, 1, 'missing-sid blueprint counts as a failed attempt');
assert.strictEqual(r3.coded, 1, 'only the paper coded');

console.log('ok');
