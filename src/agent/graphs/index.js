/**
 * Graph registry. Add new LangGraph flows here so they get:
 *   - listed on /api/graphs
 *   - runnable via `node bin/run-graph.js <name> <json-input>`
 */
'use strict';

const cycleGraph = require('../graph');
const paperhunter = require('./paperhunter');

const graphs = {
  cycle: {
    name: 'cycle',
    description: 'Daily cycle: datajohn → researchjohn → tradejohn → (HITL) → botjohn',
    run: async (input) => cycleGraph.runCycleGraph(input),
    resume: async (input) => cycleGraph.resumeCycle(input),
    state: async (threadId) => cycleGraph.listThreadState(threadId),
    nodes: ['datajohn', 'researchjohn', 'tradejohn', 'botjohn'],
    features: ['postgres-checkpoint', 'hitl-interrupt', 'conditional-routing'],
  },
  paperhunter: {
    name: 'paperhunter',
    description: 'Parallel fan-out over paper candidates (Send)',
    run: async (input) => paperhunter.runPaperHunt(input),
    nodes: ['dispatch', 'extract_one', 'reduce'],
    features: ['parallel-fanout'],
  },
};

function list() {
  return Object.values(graphs).map(({ name, description, nodes, features }) => ({
    name, description, nodes, features,
  }));
}

function get(name) { return graphs[name]; }

module.exports = { graphs, list, get };
