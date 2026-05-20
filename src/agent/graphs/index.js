/**
 * Graph registry. Add new LangGraph flows here so they get:
 *   - listed on /api/graphs
 *   - runnable via `node bin/run-graph.js <name> <json-input>`
 */
'use strict';

const cycleGraph = require('../graph');
const paperhunter = require('./paperhunter');
const dailyCycle = require('./daily-cycle');

const graphs = {
  cycle: {
    name: 'cycle',
    description: 'Daily cycle: datajohn → tradejohn → (HITL) → botjohn',
    run: async (input) => cycleGraph.runCycleGraph(input),
    resume: async (input) => cycleGraph.resumeCycle(input),
    state: async (threadId) => cycleGraph.listThreadState(threadId),
    nodes: ['datajohn', 'tradejohn', 'botjohn'],
    features: ['postgres-checkpoint', 'hitl-interrupt', 'conditional-routing'],
  },
  paperhunter: {
    name: 'paperhunter',
    description: 'Parallel fan-out over paper candidates (Send)',
    run: async (input) => paperhunter.runPaperHunt(input),
    nodes: ['dispatch', 'extract_one', 'reduce'],
    features: ['parallel-fanout'],
  },
  'daily-cycle': {
    name: 'daily-cycle',
    description: 'Unified 11-step data pipeline (replaces pipeline_orchestrator.py)',
    run: async (input) => dailyCycle.runDailyCycleGraph(input),
    resume: async (input) => dailyCycle.resumeDailyCycle(input.runDate || input),
    state: async (runDate) => dailyCycle.listThreadState(runDate),
    nodes: dailyCycle.STEPS_IN_ORDER,
    features: ['postgres-checkpoint', 'subset-filter', 'subprocess-wraps'],
  },
};

function list() {
  return Object.values(graphs).map(({ name, description, nodes, features }) => ({
    name, description, nodes, features,
  }));
}

function get(name) { return graphs[name]; }

module.exports = { graphs, list, get };
