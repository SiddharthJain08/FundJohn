const assert = require('assert');
const mod = require('../../src/agent/curators/git_strategy_ingest.js');

// validateSpec accepts a well-formed hunter_result_json, rejects missing fields.
const good = {
  strategy_id: 'S_ast_x', hypothesis_one_liner: 'hold ETF over 10mo SMA',
  signal_logic: '...', data_requirements: { required: ['prices'], optional: [] },
  universe: 'ETF_BASKET', inferred_universe_filter: null, inferred_instrument_class: 'etp',
};
assert.strictEqual(mod.validateSpec(good).ok, true);
assert.strictEqual(mod.validateSpec({ strategy_id: 'x' }).ok, false);
// extractSpec composes the parsed file into a spec via an injected runner (DI for testing)
const parsed = { strategy_id: 'S_ast_x', slug: 'x', rule_comment: 'hold ETF over 10mo SMA; source quantpedia', cited_url: 'https://quantpedia.com/x', code: 'class X(QCAlgorithm): pass' };
const fakeRunner = async () => ({ text: '```json\n' + JSON.stringify(good) + '\n```', costUsd: 0.01, error: null });
mod.extractSpec(parsed, { runner: fakeRunner }).then(spec => {
  assert.strictEqual(spec.strategy_id, 'S_ast_x');
  assert.strictEqual(spec.inferred_instrument_class, 'etp');
  console.log('ok');
});
