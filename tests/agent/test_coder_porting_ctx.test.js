const assert = require('assert');
const { buildCoderContext } = require('../../src/agent/research/research-orchestrator.js');
// git-origin spec with reference_impl → ctx carries porting fields
const ctx1 = buildCoderContext({ strategy_id: 'S_ast_x', inferred_instrument_class: 'etp', reference_impl: 'class X(QCAlgorithm): pass', reference_url: 'https://quantpedia.com/x' });
assert.ok(ctx1.REFERENCE_IMPLEMENTATION.includes('QCAlgorithm'));
assert.ok(ctx1.PORTING_GUIDE && ctx1.PORTING_GUIDE.includes('quantconnect-to-basestrategy'));
assert.strictEqual(ctx1.SOURCE_URL, 'https://quantpedia.com/x');
// paper spec without reference_impl → no porting fields (unchanged behavior)
const ctx2 = buildCoderContext({ strategy_id: 'S_p', inferred_instrument_class: 'equity' });
assert.strictEqual(ctx2.REFERENCE_IMPLEMENTATION, undefined);
console.log('ok');
