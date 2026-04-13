'use strict';

/**
 * discord-relay.js — Command routing, per-channel concurrency, and compact Discord formatting.
 *
 * Enforces max 1 active diligence run per Discord channel.
 * Queues additional requests while a run is in progress.
 * Provides compact format functions for all Discord message types.
 *
 * Usage:
 *   const relay = require('./discord-relay');
 *   relay.setFeed(operatorFeedInstance);
 *   relay.handleDiligenceRequest(message, ticker, runFn);
 */

const OperatorFeed = require('./operator-feed');

// ── Compact Discord Formatters ───────────────────────────────────────────────

/**
 * Format a diligence result as a compact Discord message (<2000 chars).
 * Full memo is sent as file attachment separately.
 *
 * @param {string} ticker
 * @param {string} verdict  — PROCEED | REVIEW | KILL
 * @param {object} parsed   — { bull, bear, mgmt, filing, revenue } parsed agent kv blocks
 * @param {object} checklist — { score, checks } from evaluateChecklist()
 * @returns {string}
 */
function formatDiligenceSummary(ticker, verdict, parsed = {}, checklist = {}) {
  const icon = verdict === 'PROCEED' ? '🟢' : verdict === 'KILL' ? '🔴' : '🟡';
  const score = checklist.score || '?/6';

  const bull     = parsed.bull?.kv    || {};
  const bear     = parsed.bear?.kv    || {};
  const mgmt     = parsed.mgmt?.kv    || {};
  const filing   = parsed.filing?.kv  || {};
  const revenue  = parsed.revenue?.kv || {};

  const bullTarget  = bull.BULL_TARGET  ? `$${(bull.BULL_TARGET.match(/\$?([\d.]+)\/share/) || [])[1] || '?'}` : '—';
  const bearTarget  = bear.BEAR_TARGET  ? `$${(bear.BEAR_TARGET.match(/\$?([\d.]+)\/share/) || [])[1] || '?'}` : '—';
  const bullUpside  = bull.UPSIDE_PCT   ? `+${bull.UPSIDE_PCT.replace('%', '')}%` : '—';
  const bearDown    = bear.DOWNSIDE_PCT ? `${bear.DOWNSIDE_PCT.replace('%', '')}%` : '—';
  const bullProb    = bull.PROBABILITY  || '—';
  const bearProb    = bear.PROBABILITY  || '—';
  const mgmtScore   = mgmt.SCORE        ? `${mgmt.SCORE}` : '—';
  const mgmtVerdict = mgmt.VERDICT      ? mgmt.VERDICT.replace('_', ' ') : '—';
  const filingNet   = filing.NET_ASSESSMENT || '—';
  const revScore    = revenue.SCORE     ? `${revenue.SCORE}` : '—';
  const revVerdict  = revenue.VERDICT   || '—';
  const signals     = checklist.signals?.filter(s => s.startsWith('kill_') || s.startsWith('mgmt_')).join(', ') || 'none';

  return [
    `${icon} **${ticker}** — **${verdict}** (${score} passed)`,
    `Bull: ${bullTarget} (${bullUpside}) [${bullProb}] | Bear: ${bearTarget} (${bearDown}) [${bearProb}]`,
    `Mgmt: ${mgmtScore}/100 ${mgmtVerdict} | Filing: ${filingNet} | Revenue: ${revScore}/100 ${revVerdict}`,
    `Signals: ${signals}`,
    `📎 Full memo attached`,
  ].join('\n');
}

/**
 * Format a trade result as a compact Discord message.
 */
function formatTradeSummary(ticker, tradeData = {}) {
  const quant  = tradeData.quant  || {};
  const risk   = tradeData.risk   || {};
  const timing = tradeData.timing || {};

  const direction = quant.DIRECTION || 'UNKNOWN';
  const icon = direction === 'LONG' ? '📈' : direction === 'SHORT' ? '📉' : '⏸';

  const entry    = quant.ENTRY_ZONE      || '—';
  const stop     = quant.STOP            || '—';
  const t1       = quant.TARGET_1        ? quant.TARGET_1.split(' ')[0] : '—';
  const t2       = quant.TARGET_2        ? quant.TARGET_2.split(' ')[0] : '—';
  const size     = risk.ADJUSTED_SIZE    || quant.POSITION_SIZE_PCT || '—';
  const rr       = quant.RISK_REWARD     || '—';
  const ev       = quant.EV_PCT          ? `${quant.EV_PCT}%` : '—';
  const riskVerd = risk.RISK_VERDICT     || '—';
  const signal   = timing.SIGNAL         || '—';
  const entryType = timing.ENTRY_TYPE    || '—';
  const entryDetail = timing.ENTRY_DETAIL || '—';

  const riskIcon    = riskVerd === 'APPROVED' ? '✅' : riskVerd === 'REDUCED' ? '⚠️' : '🛑';
  const timingIcon  = signal === 'GO' ? '🎯' : signal === 'WAIT' ? '⏳' : '❌';

  return [
    `${icon} **${ticker}** — **${direction}**`,
    `Entry: ${entry} | Stop: ${stop} | T1: ${t1} | T2: ${t2}`,
    `Size: ${size}% | R:R ${rr} | EV: ${ev}`,
    `${riskIcon} Risk: ${riskVerd} | ${timingIcon} Timing: ${signal} — ${entryType} ${entryDetail}`,
    `📎 Full trade report attached`,
  ].join('\n');
}

/**
 * Format a portfolio snapshot.
 */
function formatPortfolioSummary(state = {}) {
  const longPct  = state.long_pct   ?? '—';
  const shortPct = state.short_pct  ?? '—';
  const cashPct  = state.cash_pct   ?? '—';
  const heat     = state.heat       ?? '—';
  const pnl      = state.open_pnl   != null ? `${state.open_pnl > 0 ? '+' : ''}$${state.open_pnl.toLocaleString()}` : '—';
  const pnlPct   = state.open_pnl_pct != null ? `(${state.open_pnl_pct > 0 ? '+' : ''}${state.open_pnl_pct}%)` : '';
  const date     = new Date().toISOString().slice(0, 10);

  const positions = (state.positions || [])
    .slice(0, 6)
    .map(p => `${p.ticker} ${p.size_pct}% (${p.pnl_pct > 0 ? '+' : ''}${p.pnl_pct}%)`)
    .join(', ');

  return [
    `📊 **Portfolio** — ${date}`,
    `Long: ${longPct}% | Short: ${shortPct}% | Cash: ${cashPct}%`,
    `Heat: ${heat}% | Open P&L: ${pnl} ${pnlPct}`,
    positions ? `Positions: ${positions}` : 'No open positions',
  ].join('\n');
}

/**
 * Format a watchlist scan result.
 */
function formatWatchlistScan(results = []) {
  const lines = results.map(r => {
    const icon = r.signal === 'GO' ? '🎯' : r.signal === 'WAIT' ? '⏳' : '❌';
    return `${icon} **${r.ticker}**: ${r.signal}${r.detail ? ` — ${r.detail}` : ''}`;
  });
  return [`🔍 **Watchlist scan** — ${results.length} tickers checked`, ...lines].join('\n');
}

class DiscordRelay {
  constructor() {
    // channelId → { active: boolean, queue: Array<{ message, ticker, runFn, resolve, reject }> }
    this._channels = new Map();
    this._feed     = null;
  }

  /**
   * Attach an OperatorFeed instance for notifications.
   * @param {OperatorFeed} feed
   */
  setFeed(feed) {
    this._feed = feed;
  }

  /**
   * Handle a /diligence request from Discord.
   * If the channel is busy, queues the request and notifies the user.
   *
   * @param {import('discord.js').Message} message
   * @param {string} ticker
   * @param {function(ticker: string): Promise<string>} runFn  — resolves with raw orchestrator output
   * @returns {Promise<string>} — resolves with raw orchestrator stdout
   */
  handleDiligenceRequest(message, ticker, runFn) {
    const channelId = message.channelId;
    const state     = this._getChannelState(channelId);

    return new Promise((resolve, reject) => {
      if (!state.active) {
        // Channel is free — run immediately
        this._executeRequest(channelId, message, ticker, runFn, resolve, reject);
      } else {
        // Channel busy — queue the request
        const queuePos = state.queue.length + 1;
        message.reply({
          content: `⏳ Diligence already running in this channel. **${ticker}** queued at position ${queuePos}.`,
          allowedMentions: { repliedUser: false },
        }).catch(() => {});
        state.queue.push({ message, ticker, runFn, resolve, reject });
      }
    });
  }

  /**
   * Check if a channel has an active run.
   */
  isChannelBusy(channelId) {
    return this._channels.get(channelId)?.active || false;
  }

  /**
   * Get queue depth for a channel.
   */
  getQueueDepth(channelId) {
    return this._channels.get(channelId)?.queue.length || 0;
  }

  /**
   * Cancel all queued requests for a channel (e.g. on bot shutdown).
   */
  clearChannel(channelId) {
    const state = this._channels.get(channelId);
    if (!state) return;
    state.queue.forEach(({ reject }) => reject(new Error('Queue cleared')));
    state.queue = [];
    state.active = false;
  }

  // ── Internal ────────────────────────────────────────────────────────────────

  _getChannelState(channelId) {
    if (!this._channels.has(channelId)) {
      this._channels.set(channelId, { active: false, queue: [] });
    }
    return this._channels.get(channelId);
  }

  async _executeRequest(channelId, message, ticker, runFn, resolve, reject) {
    const state = this._getChannelState(channelId);
    state.active = true;

    // Update operator-feed channel target if not already set
    if (this._feed && message.channel) {
      this._feed.setChannel(message.channel);
    }

    try {
      const result = await runFn(ticker);
      resolve(result);
    } catch (err) {
      reject(err);
    } finally {
      state.active = false;
      this._processQueue(channelId);
    }
  }

  _processQueue(channelId) {
    const state = this._getChannelState(channelId);
    if (state.queue.length === 0) return;

    const next = state.queue.shift();
    next.message.channel.send({
      content: `▶️ Starting queued diligence for **${next.ticker}**...`,
      allowedMentions: { repliedUser: false },
    }).catch(() => {});

    this._executeRequest(channelId, next.message, next.ticker, next.runFn, next.resolve, next.reject);
  }
}

const relay = new DiscordRelay();
module.exports = relay;

// Attach formatters as static methods for convenience
module.exports.formatDiligenceSummary = formatDiligenceSummary;
module.exports.formatTradeSummary     = formatTradeSummary;
module.exports.formatPortfolioSummary = formatPortfolioSummary;
module.exports.formatWatchlistScan    = formatWatchlistScan;
