'use strict';

/**
 * agent-bus.js — Inter-agent event bus for the OpenClaw diligence network.
 *
 * Tracks agent states, detects kill signals, and emits coordination events
 * that the orchestrator and Discord bot can react to in real time.
 *
 * Events emitted:
 *   'agent-start'     { runId, agentId, agentName, ticker, startTime }
 *   'agent-complete'  { runId, agentId, agentName, ticker, elapsed, error }
 *   'agent-timeout'   { runId, agentId, agentName, ticker, elapsed }
 *   'kill-signal'     { runId, agentId, agentName, ticker, signal, evidence }
 *   'all-complete'    { runId, ticker, results, elapsed }
 *   'run-error'       { runId, ticker, error }
 */

const { EventEmitter } = require('events');

class AgentBus extends EventEmitter {
  constructor() {
    super();
    // runId → { ticker, agents: { agentId → state }, startTime, expectedCount }
    this._runs = new Map();
  }

  /**
   * Register a new diligence run.
   * @param {string} runId
   * @param {string} ticker
   * @param {string[]} agentIds  — ordered list of agent IDs in this run
   */
  registerRun(runId, ticker, agentIds) {
    this._runs.set(runId, {
      ticker,
      startTime: Date.now(),
      expectedCount: agentIds.length,
      agents: Object.fromEntries(agentIds.map(id => [id, { status: 'pending', startTime: null, endTime: null, elapsed: null, killSignal: null }])),
      killSignals: [],
      completedCount: 0,
    });
  }

  /**
   * Mark an agent as started.
   */
  agentStarted(runId, agentId, agentName) {
    const run = this._runs.get(runId);
    if (!run) return;
    run.agents[agentId] = { ...run.agents[agentId], status: 'running', startTime: Date.now() };
    this.emit('agent-start', { runId, agentId, agentName, ticker: run.ticker, startTime: run.agents[agentId].startTime });
  }

  /**
   * Mark an agent as complete.
   * @param {string} runId
   * @param {string} agentId
   * @param {string} agentName
   * @param {string} output     — agent stdout
   * @param {boolean} error     — true if agent errored or timed out
   * @param {number} elapsed    — ms
   * @param {boolean} timedOut
   */
  agentFinished(runId, agentId, agentName, output, error, elapsed, timedOut = false) {
    const run = this._runs.get(runId);
    if (!run) return;

    const status = timedOut ? 'timeout' : error ? 'error' : 'complete';
    run.agents[agentId] = { ...run.agents[agentId], status, endTime: Date.now(), elapsed };
    run.completedCount++;

    // Detect kill signals in the agent output
    const killSignals = this._extractKillSignals(output);
    if (killSignals.length > 0) {
      killSignals.forEach(({ signal, evidence }) => {
        run.killSignals.push({ agentId, agentName, signal, evidence });
        this.emit('kill-signal', { runId, agentId, agentName, ticker: run.ticker, signal, evidence });
      });
    }

    if (timedOut) {
      this.emit('agent-timeout', { runId, agentId, agentName, ticker: run.ticker, elapsed });
    } else {
      this.emit('agent-complete', { runId, agentId, agentName, ticker: run.ticker, elapsed, error });
    }

    // Check if all agents are done
    if (run.completedCount >= run.expectedCount) {
      const totalElapsed = Date.now() - run.startTime;
      this.emit('all-complete', {
        runId,
        ticker: run.ticker,
        agents: run.agents,
        killSignals: run.killSignals,
        elapsed: totalElapsed,
      });
    }
  }

  /**
   * Signal a fatal run-level error (e.g. orchestrator crash).
   */
  runError(runId, ticker, error) {
    this.emit('run-error', { runId, ticker, error: error.message || String(error) });
    this._runs.delete(runId);
  }

  /**
   * Get current state snapshot for a run.
   */
  getRunState(runId) {
    return this._runs.get(runId) || null;
  }

  /**
   * Clean up a completed run.
   */
  clearRun(runId) {
    this._runs.delete(runId);
  }

  // ── Internal ────────────────────────────────────────────────────────────────

  /**
   * Parse kill signal lines from agent output.
   * Matches: ⚠️ KILL SIGNAL: <signal> — <evidence>
   *          ⚠️ MGMT SIGNAL: <signal> — <evidence>
   *          ⚠️ BULL SIGNAL: <signal> — <evidence>
   */
  _extractKillSignals(output) {
    if (!output) return [];
    const signals = [];
    const re = /⚠️\s+(KILL|MGMT|BULL)\s+SIGNAL:\s+(.+?)(?:\s+—\s+(.+))?$/gm;
    let match;
    while ((match = re.exec(output)) !== null) {
      signals.push({
        signal:   `${match[1]} SIGNAL: ${match[2].trim()}`,
        evidence: match[3]?.trim() || '',
      });
    }
    return signals;
  }
}

module.exports = new AgentBus();
