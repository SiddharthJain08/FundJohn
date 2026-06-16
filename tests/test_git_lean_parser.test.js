const assert = require('assert');
const fs = require('fs');
const { parseLeanFile } = require('../src/agent/curators/git_strategy_ingest.js');

const text = fs.readFileSync(__dirname + '/fixtures/lean_asset_class_trend_following.py', 'utf8');
const p = parseLeanFile(text, 'asset-class-trend-following.py');
assert.strictEqual(p.slug, 'asset_class_trend_following');
assert.strictEqual(p.strategy_id, 'S_ast_asset_class_trend_following');
assert.ok(p.cited_url && p.cited_url.includes('quantpedia.com'));
assert.ok(/10 month|SMA|equal weight/i.test(p.rule_comment)); // rule captured from comment
assert.ok(p.code.includes('QCAlgorithm'));                    // raw code retained (for porting only)
console.log('ok');
