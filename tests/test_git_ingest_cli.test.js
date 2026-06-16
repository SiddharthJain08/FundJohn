'use strict';
// Part 1 (Blueprint Fast Lane Phase 2.4): `run_mastermind.js --mode git-ingest`
// must dispatch to git_strategy_ingest.run({dryRun, incremental, repo}) and
// aggregate {inserted,skipped,errored}. Network/DB-free: the per-repo run is
// exercised via DI stubs (mirrors tests/test_git_ingest_run.test.js), and the
// CLI wiring is asserted statically (the IIFE in run_mastermind.js would
// otherwise auto-run a real clone on require).
const assert = require('assert');
const fs   = require('fs');
const os   = require('os');
const path = require('path');

const ingest = require('../src/agent/curators/git_strategy_ingest.js');

// ── 1. Module dispatch: run({dryRun,incremental,repo}) returns the summary the
//      CLI prints, using only injected deps (no clone, no Sonnet, no Postgres).
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gitcli-'));
fs.mkdirSync(path.join(dir, 'static', 'strategies'), { recursive: true });
fs.copyFileSync(
  path.join(__dirname, 'fixtures/lean_asset_class_trend_following.py'),
  path.join(dir, 'static/strategies/asset-class-trend-following.py'));

const inserted = [];
const deps = {
  cloneFn: async () => dir,
  runner: async () => ({
    text: '```json\n{"strategy_id":"S_ast_asset_class_trend_following",'
        + '"hypothesis_one_liner":"x","signal_logic":"y",'
        + '"data_requirements":{"required":["prices"],"optional":[]},'
        + '"universe":"ETF","inferred_universe_filter":null,'
        + '"inferred_instrument_class":"etp"}\n```',
    costUsd: 0.01, error: null }),
  existsFn: async () => false,
  insertFn: async (row) => { inserted.push(row); },
};
const repo = { name: 'fixture', repo: 'r', branch: 'main',
  strategies_glob: 'static/strategies/*.py', file_url_template: 'https://x/{file}' };

ingest.run({ dryRun: false, incremental: true, deps, repo }).then((res) => {
  assert.strictEqual(res.inserted, 1, 'inserted one fixture strategy');
  assert.strictEqual(res.skipped, 0);
  assert.strictEqual(res.errored, 0);
  assert.strictEqual(inserted[0].origin, 'git_blueprint');

  // ── 2. CLI wiring: --mode git-ingest is recognized and routed to the module.
  const cli = fs.readFileSync(
    path.join(__dirname, '../src/agent/curators/run_mastermind.js'), 'utf8');
  assert.ok(/mode === 'git-ingest'\)\s*return runGitIngest\(\)/.test(cli),
    'dispatcher must route git-ingest → runGitIngest()');
  assert.ok(/require\('\.\/git_strategy_ingest'\)/.test(cli),
    'runGitIngest must require ./git_strategy_ingest');
  assert.ok(/run\(\{\s*dryRun,\s*incremental,\s*repo\s*\}\)/.test(cli),
    'runGitIngest must call run({dryRun, incremental, repo}) with passthrough');
  assert.ok(/getArg\('--incremental'/.test(cli), '--incremental flag parsed');
  assert.ok(/getArg\('--dry-run'/.test(cli), '--dry-run flag parsed');
  // git-ingest must NOT appear in the Unknown-mode error list.
  assert.ok(/Expected:[^\n]*git-ingest/.test(cli),
    'git-ingest listed as a known mode');

  console.log('ok');
}).catch((e) => { console.error(e); process.exit(1); });
