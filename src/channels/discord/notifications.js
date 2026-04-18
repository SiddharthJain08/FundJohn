'use strict';

/**
 * Operator notification types with consistent formatting.
 * All notifications route through Discord via agentPersonas where appropriate.
 *
 * Channel routing:
 *   botjohn-log     — system events, errors, startup
 *   research-feed   — signal synthesis + diligence progress (ResearchDesk persona)
 *   strategy-memos  — daily strategy memos + diligence verdicts (DataBot / ResearchDesk)
 *   trade-signals   — trade pipeline, risk verdicts, approvals (TradeDesk persona)
 *   trade-reports   — final trade reports + BLOCKED vetoes (TradeDesk persona)
 */

let _client = null;
let _logChannelName   = 'botjohn-log';
let _agentPersonas    = null;  // injected at startup from bot.js

function init(client, opts = {}) {
  _client = client;
  if (opts.logChannel)    _logChannelName = opts.logChannel;
  if (opts.agentPersonas) _agentPersonas  = opts.agentPersonas;
}

async function getChannel(name) {
  if (!_client) return null;
  return _client.channels.cache.find((c) => c.name === name) || null;
}

async function post(channelName, text) {
  const ch = await getChannel(channelName);
  if (!ch) return;
  const chunks = [];
  let t = String(text);
  while (t.length > 0) {
    const at = t.length <= 1990 ? t.length : t.lastIndexOf('\n', 1990) || 1990;
    chunks.push(t.slice(0, at));
    t = t.slice(at).replace(/^\n/, '');
  }
  for (const chunk of chunks) await ch.send({ content: chunk });
}

// Post via agent persona webhook if available, fall back to direct channel post
async function personaPost(agentId, channelKey, text) {
  if (_agentPersonas) {
    return _agentPersonas.post(agentId, channelKey, text).catch(() => post(channelKey, text));
  }
  return post(channelKey, text);
}

// Notification types — routed to appropriate channels/personas
const notify = {
  // Research flow → ResearchDesk in #research-feed
  diligenceStarted:   (ticker, n) => personaPost('researchdesk', 'research-feed', `🔬 Diligence started for **${ticker}** — spawning ${n} subagents`),
  subagentComplete:   (type, ticker, duration) => personaPost('researchdesk', 'research-feed', `✅ **${capitalize(type)}** complete for **${ticker}** [${duration}s]`),
  validationFailed:   (ticker, errors) => personaPost('researchdesk', 'research-feed', `⛔ DATA VALIDATION FAILED for **${ticker}**: ${errors}`),
  diligenceComplete:  (ticker, verdict, score) => personaPost('researchdesk', 'strategy-memos', `🦞 **${ticker}** — **${verdict}** (${score})`),

  // Trade flow → TradeDesk in #trade-signals / #trade-reports
  tradePipeline:      (ticker) => personaPost('tradedesk', 'trade-signals', `📐 Trade pipeline: Compute → Analyst → Report | **${ticker}**`),
  riskDecision:       (verdict, reason) => personaPost('tradedesk', 'trade-signals', `🛡️ Risk: **${verdict}** — ${reason}`),
  tradeReview:        (ticker, reason, tradeId) => personaPost('tradedesk', 'trade-reports', `🔍 TRADE REVIEW REQUIRED: **${ticker}** — ${reason} | Approve: \`!john /approve ${tradeId}\``),
  pendingReview:      (ticker, tradeId) => personaPost('tradedesk', 'trade-reports', `🕐 PENDING REVIEW: **${ticker}** — \`!john /approve ${tradeId}\` or \`!john /reject ${tradeId}\``),

  // Risk/signal flags → TradeDesk (inline with trade flow, no separate alerts channel)
  signalFlagged:      (agent, signal, ticker) => personaPost('tradedesk', 'trade-signals', `⚠️ **${agent}** flagged **${signal}** for **${ticker}**`),
  portfolioStale:     (hours) => post(_logChannelName, `⚠️ PORTFOLIO STATE STALE — ${hours} hours. Update portfolio.json before trade execution.`),
  watchlistAlert:     (ticker, signal) => personaPost('tradedesk', 'trade-signals', `🎯 WATCHLIST ALERT: **${ticker}** signal changed to **${signal}**`),

  // System → botjohn-log
  timingSignal:       (ticker, signal) => post(_logChannelName, `🎯 **${ticker}** timing: **${signal}**`),
  contextCompacting:  (pct) => post(_logChannelName, `📊 Context: ${pct}% utilized — compacting`),
  rateLimitFallback:  (provider, fallback) => post(_logChannelName, `⚡ Rate limit: **${provider}** exhausted — falling back to **${fallback}**`),
};

async function postStartup(client) {
  init(client);
  const ch = await getChannel(_logChannelName);
  if (!ch) return;
  await ch.send({ content: `\`${new Date().toISOString()}\` 🦞 BotJohn v2 (PTC Architecture) online` });
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

async function notifyStrategyReport(channel, report) {
    const embed = {
        color: 0x00d4aa,
        title: `📊 New Strategy Report: ${report.strategy_name}`,
        description: report.summary,
        fields: [
            { name: 'Sharpe',          value: report.sharpe.toFixed(2),               inline: true },
            { name: 'Annual Return',   value: `${report.annual_return.toFixed(1)}%`,  inline: true },
            { name: 'Max Drawdown',    value: `${report.max_drawdown.toFixed(1)}%`,   inline: true },
            { name: 'Win Rate',        value: `${(report.win_rate*100).toFixed(0)}%`, inline: true },
            { name: 'Profit Factor',   value: report.profit_factor.toFixed(2),        inline: true },
            { name: 'Walk-Forward',    value: report.walk_forward_label,              inline: true },
            { name: 'Tier',            value: report.tier,                            inline: true },
            { name: 'Complexity',      value: report.complexity,                      inline: true },
            { name: 'Backtest Period', value: `${report.start_date} → ${report.end_date}`, inline: false },
        ],
        footer: { text: `Session: ${report.session_id} | Report ID: ${report.report_id}` },
        timestamp: new Date().toISOString(),
    };
    const msg = await channel.send({
        content: `@here 🦞 **Strategy report ready for review.** React ✅ to approve or ❌ to reject.`,
        embeds: [embed],
        files: report.attachments || [],
    });
    await msg.react('✅');
    await msg.react('❌');
    return msg.id;
}

async function notifyEmergencyAlert(alert) {
    // Accepts alert object OR a channel object + alert (backward compat)
    if (alert && typeof alert.send === 'function') {
        // Old call signature: notifyEmergencyAlert(channel, alert) — ignore channel, use agentPersonas
        alert = arguments[1];
    }
    const emojis = { CRITICAL: '🚨', HIGH: '⚠️', MEDIUM: '⚡' };
    const e = emojis[alert.severity] || '⚠️';
    const text = `${e} **EMERGENCY ALERT — ${alert.alert_type}**\n` +
        `Ticker: ${alert.ticker || 'Portfolio-wide'} | Severity: **${alert.severity}**\n` +
        `${alert.description}\n` +
        Object.entries(alert.evidence || {}).slice(0, 4).map(([k, v]) => `• ${k}: ${v}`).join('\n');

    // Route to #trade-reports via TradeDesk (no separate alerts channel)
    await personaPost('tradedesk', 'trade-reports', `@here ${text}`).catch(() => {});
}

/**
 * Post DataBot strategy execution memo to #strategy-memos.
 */
async function notifyStrategyMemo(memo) {
    await personaPost('databot', 'strategy-memos', memo).catch(() => {});
}

/**
 * Post ResearchDesk signal synthesis to #research-feed.
 */
async function notifySignalSynthesis(synthesis) {
    await personaPost('researchdesk', 'research-feed', synthesis).catch(() => {});
}

/**
 * Post a strategist status message to #botjohn-log.
 * Callers pass a plain string — this replaces the old (channel, status) signature.
 */
async function notifyStrategistStatus(msg) {
    if (typeof msg !== 'string') msg = JSON.stringify(msg);
    await post(_logChannelName, msg).catch(() => {});
}

/**
 * Post execution engine signal summary to #trade-signals via TradeDesk.
 * Called by runner.js after engine run.
 */
async function notifyEngineSignals(report) {
    await personaPost('tradedesk', 'trade-signals', report).catch(() => {});
}

async function notifyPositionRecommendation(rec) {
    const label = rec.action || 'REC';
    await personaPost('tradedesk', 'position-recommendations',
        `📋 **${label}** — ${rec.ticker} | ${rec.rationale}`).catch(() => {});
}

module.exports = {
    init,
    notify,
    postStartup,
    notifyStrategyReport,
    notifyEmergencyAlert,
    notifyStrategistStatus,
    notifyEngineSignals,
    notifyStrategyMemo,
    notifySignalSynthesis,
    notifyPositionRecommendation,
};
