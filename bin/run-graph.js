#!/usr/bin/env node
/**
 * CLI runner for FundJohn LangGraph flows.
 *
 *   node bin/run-graph.js list
 *   node bin/run-graph.js cycle '{"cycleDate":"2026-04-22","memoDir":"/tmp/memos","reportPath":"/tmp/r.md"}'
 *   node bin/run-graph.js cycle:resume '{"threadId":"abc","approval":"approved"}'
 *   node bin/run-graph.js cycle:state <threadId>
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const { graphs, list } = require('../src/agent/graphs');

async function main() {
  const [cmd, argJson] = process.argv.slice(2);
  if (!cmd || cmd === 'list') {
    console.log(JSON.stringify(list(), null, 2));
    process.exit(0);
  }
  const [name, sub] = cmd.split(':');
  const g = graphs[name];
  if (!g) { console.error(`unknown graph: ${name}`); process.exit(1); }

  let arg;
  try { arg = argJson ? JSON.parse(argJson) : {}; }
  catch { arg = argJson; }

  let out;
  if (!sub || sub === 'run') {
    out = await g.run(arg);
  } else if (sub === 'resume' && g.resume) {
    out = await g.resume(arg);
  } else if (sub === 'state' && g.state) {
    out = await g.state(arg);
  } else {
    console.error(`unknown subcommand: ${cmd}`);
    process.exit(1);
  }
  console.log(JSON.stringify(out, null, 2));
  process.exit(0);
}

main().catch((e) => { console.error('ERROR', e); process.exit(1); });
