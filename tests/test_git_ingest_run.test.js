const assert = require('assert');
const fs = require('fs'); const os = require('os'); const path = require('path');
const mod = require('../src/agent/curators/git_strategy_ingest.js');

// Build a tiny local "repo" dir of fixture strategy files.
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gitfix-'));
fs.mkdirSync(path.join(dir, 'static', 'strategies'), { recursive: true });
fs.copyFileSync(__dirname + '/fixtures/lean_asset_class_trend_following.py', path.join(dir, 'static/strategies/asset-class-trend-following.py'));

const inserted = [];
const deps = {
  cloneFn: async () => dir,                          // skip real clone
  runner: async () => ({ text: '```json\n{"strategy_id":"S_ast_asset_class_trend_following","hypothesis_one_liner":"x","signal_logic":"y","data_requirements":{"required":["prices"],"optional":[]},"universe":"ETF","inferred_universe_filter":null,"inferred_instrument_class":"etp"}\n```', costUsd: 0.01, error: null }),
  existsFn: async (url) => false,                     // not yet ingested
  insertFn: async (row) => { inserted.push(row); },
};
mod.run({ dryRun: false, deps, repo: { strategies_glob: 'static/strategies/*.py', file_url_template: 'https://x/{file}', branch: 'main', repo: 'r' } }).then(res => {
  assert.strictEqual(inserted.length, 1);
  assert.strictEqual(inserted[0].origin, 'git_blueprint');
  assert.ok(inserted[0].source_url.includes('asset-class-trend-following.py'));
  assert.strictEqual(inserted[0].hunter_result_json.inferred_instrument_class, 'etp');
  // idempotency: re-run with existsFn → true inserts nothing
  const ins2 = [];
  return mod.run({ dryRun: false, repo: { strategies_glob: 'static/strategies/*.py', file_url_template: 'https://x/{file}', branch:'main', repo:'r' },
    deps: { ...deps, existsFn: async () => true, insertFn: async (r) => ins2.push(r) } }).then(() => {
      assert.strictEqual(ins2.length, 0); console.log('ok');
    });
});
