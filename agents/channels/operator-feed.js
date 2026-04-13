'use strict';

/**
 * operator-feed.js — Discord notification queue for BotJohn operator updates.
 *
 * Enforces a 2-second rate limit between Discord messages to avoid API throttling.
 * Handles 6 notification types with consistent formatting.
 *
 * Notification types:
 *   RUN_STARTED     — diligence run kicked off, agents spawning
 *   AGENT_COMPLETE  — individual agent finished (success or error)
 *   KILL_SIGNAL     — kill criterion detected mid-run
 *   RUN_COMPLETE    — all agents done, verdict ready
 *   RUN_ERROR       — orchestrator fatal error
 *   AGENT_TIMEOUT   — individual agent hit 5-min cap
 */

const RATE_LIMIT_MS = 2000; // minimum gap between Discord sends

// Routing table: notification type → channel key name (from channel-map.json)
const TYPE_CHANNEL_MAP = {
  RUN_STARTED:    'research-feed',
  AGENT_COMPLETE: 'research-feed',
  AGENT_TIMEOUT:  'research-feed',
  AGENT_PREVIEW:  'research-feed',
  KILL_SIGNAL:    'alerts',
  RUN_COMPLETE:   'diligence-memos',
  RUN_ERROR:      'alerts',
  DESK_START:     'agent-chat',
  AGENT_STEP:     'agent-chat',
  TRADE_ALERT:    'alerts',
  EXIT_ALERT:     'alerts',
  RISK_VETO:      'risk-desk',
  DESK_COMPLETE:  'trade-reports',
};

class OperatorFeed {
  /**
   * @param {import('discord.js').TextChannel | null} channel  — fallback Discord channel
   * @param {import('../../../johnbot/channel-map') | null} channelMap — optional multi-channel router
   * @param {import('discord.js').Client | null} client — Discord client for channel lookup
   */
  constructor(channel = null, channelMap = null, client = null) {
    this._channel    = channel;
    this._channelMap = channelMap;
    this._client     = client;
    this._queue      = [];
    this._timer      = null;
    this._lastSent   = 0;
  }

  /** Set or swap the fallback Discord channel target. */
  setChannel(channel) {
    this._channel = channel;
  }

  /** Set the channel map and client for multi-channel routing. */
  setChannelMap(channelMap, client) {
    this._channelMap = channelMap;
    this._client     = client;
  }

  /** Resolve the target channel for a notification type. */
  _resolveChannel(type) {
    // Try type-specific channel from the map
    if (this._channelMap && this._client) {
      const key = TYPE_CHANNEL_MAP[type];
      if (key) {
        const ch = this._channelMap.getChannel(this._client, key);
        if (ch) return ch;
      }
    }
    // Fall back to default channel
    return this._channel;
  }

  // ── Notification builders ───────────────────────────────────────────────────

  runStarted(ticker, agentCount, runId) {
    this._enqueue({
      type: 'RUN_STARTED',
      content: `🦞 **Diligence started — ${ticker}**\nRun ID: \`${runId}\` | Spawning ${agentCount} agents in parallel...`,
    });
  }

  agentComplete(ticker, agentName, emoji, elapsed, error) {
    const icon    = error ? '⚠️' : '✅';
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    this._enqueue({
      type: 'AGENT_COMPLETE',
      content: `${icon} **${agentName}** complete for ${ticker} (${elapsedStr})`,
    });
  }

  agentTimeout(ticker, agentName) {
    this._enqueue({
      type: 'AGENT_TIMEOUT',
      content: `⏱️ **${agentName}** timed out on ${ticker} (5m cap hit)`,
    });
  }

  killSignal(ticker, agentName, signal, evidence) {
    const evidenceStr = evidence ? `\n> ${evidence.slice(0, 200)}` : '';
    this._enqueue({
      type: 'KILL_SIGNAL',
      content: `🛑 **KILL SIGNAL detected — ${ticker}**\nAgent: ${agentName} | Signal: ${signal}${evidenceStr}`,
    });
  }

  runComplete(ticker, verdict, elapsed, memoFile) {
    const icon     = verdict === 'PROCEED' ? '✅' : verdict === 'KILL' ? '🛑' : '⚠️';
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    const fileStr  = memoFile ? `\nMemo: \`${memoFile}\`` : '';
    this._enqueue({
      type: 'RUN_COMPLETE',
      content: `${icon} **Diligence complete — ${ticker} — VERDICT: ${verdict}**\nElapsed: ${elapsedStr}${fileStr}`,
    });
  }

  runError(ticker, errorMessage) {
    this._enqueue({
      type: 'RUN_ERROR',
      content: `🔥 **Orchestrator error — ${ticker}**\n\`\`\`\n${errorMessage.slice(0, 500)}\n\`\`\``,
    });
  }

  // ── Quant Trading Desk notifications (types 7–10) ─────────────────────────

  tradeAlert(ticker, action, price, sizePct, rrRatio) {
    this._enqueue({
      type: 'TRADE_ALERT',
      content: `📡 **TRADE ALERT — ${ticker}**: ${action} at $${price}, ${sizePct}% size, R/R ${rrRatio}:1 — see report`,
    });
  }

  exitAlert(ticker, reason, urgency) {
    this._enqueue({
      type: 'EXIT_ALERT',
      content: `🚨 **EXIT ALERT — ${ticker}**: ${reason} — urgency: ${urgency}`,
    });
  }

  riskVeto(ticker, reason) {
    this._enqueue({
      type: 'RISK_VETO',
      content: `🛡️ **RISK VETO — ${ticker}**: ${reason}`,
    });
  }

  deskComplete(signalCount, approved, rejected) {
    this._enqueue({
      type: 'DESK_COMPLETE',
      content: `📊 Trade scan complete — ${signalCount} signal(s) | ${approved} approved | ${rejected} rejected`,
    });
  }

  // ── Queue management ────────────────────────────────────────────────────────

  _enqueue(notification) {
    this._queue.push(notification);
    if (!this._timer) {
      this._scheduleNext();
    }
  }

  _scheduleNext() {
    const now  = Date.now();
    const wait = Math.max(0, this._lastSent + RATE_LIMIT_MS - now);
    this._timer = setTimeout(() => this._flush(), wait);
  }

  async _flush() {
    this._timer = null;
    if (this._queue.length === 0) return;

    const notification = this._queue.shift();
    this._lastSent = Date.now();

    const target = this._resolveChannel(notification.type);

    if (target) {
      try {
        if (notification.attachment) {
          await target.send({ content: notification.content, files: [notification.attachment] });
        } else {
          await target.send({ content: notification.content });
        }
      } catch (err) {
        // Non-fatal — log but don't crash the feed
        console.error(`[OperatorFeed] Send error (${notification.type} → ${target.name}):`, err.message);
      }
    } else {
      // No channel configured — log to stdout for debugging
      console.log(`[OperatorFeed][${notification.type}] ${notification.content.replace(/\n/g, ' | ')}`);
    }

    if (this._queue.length > 0) {
      this._scheduleNext();
    }
  }

  /** Drain the queue immediately (e.g. on shutdown). */
  async drain() {
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    while (this._queue.length > 0) {
      const notification = this._queue.shift();
      if (this._channel) {
        try {
          await this._channel.send({ content: notification.content });
        } catch { /* ignore on drain */ }
      }
    }
  }
}

module.exports = OperatorFeed;
