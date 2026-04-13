'use strict';

/**
 * trade-feed.js — Discord notification queue for trade pipeline updates.
 *
 * Separate from operator-feed.js (which handles research/diligence updates).
 * Same 2-second rate-limit queue pattern.
 *
 * Notification types:
 *   PIPELINE_START   — trade pipeline kicked off
 *   QUANT_COMPLETE   — Quant agent finished
 *   RISK_COMPLETE    — Risk agent finished (may include BLOCKED)
 *   TIMING_COMPLETE  — Timing agent finished
 *   FINAL_REPORT     — full trade report ready (with attachment)
 *   PIPELINE_ERROR   — fatal pipeline error
 */

const fs   = require('fs');
const path = require('path');

const RATE_LIMIT_MS = 2000;

const TYPE_CHANNEL_MAP = {
  PIPELINE_START:  'trade-signals',
  QUANT_COMPLETE:  'trade-signals',
  RISK_COMPLETE:   'risk-desk',
  TIMING_COMPLETE: 'entry-timing',
  FINAL_REPORT:    'trade-reports',
  PIPELINE_ERROR:  'alerts',
};

class TradeFeed {
  /**
   * @param {import('../../../johnbot/channel-map') | null} channelMap
   * @param {import('discord.js').Client | null} client
   */
  constructor(channelMap = null, client = null) {
    this._channelMap = channelMap;
    this._client     = client;
    this._queue      = [];
    this._timer      = null;
    this._lastSent   = 0;
  }

  setChannelMap(channelMap, client) {
    this._channelMap = channelMap;
    this._client     = client;
  }

  _resolveChannel(type) {
    if (this._channelMap && this._client) {
      const key = TYPE_CHANNEL_MAP[type];
      if (key) {
        const ch = this._channelMap.getChannel(this._client, key);
        if (ch) return ch;
      }
    }
    return null;
  }

  // ── Notification builders ───────────────────────────────────────────────────

  pipelineStart(ticker, memoFile) {
    this._enqueue({
      type: 'PIPELINE_START',
      content: `📐 **Trade pipeline started — ${ticker}**\nMemo: \`${memoFile}\` | Running Quant → Risk → Timing...`,
    });
  }

  quantComplete(ticker, recommendation, evRatio, sizePct, negativeEV, elapsed) {
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    if (negativeEV || recommendation === 'PASS') {
      this._enqueue({
        type: 'QUANT_COMPLETE',
        content: `📐 **Quant: PASS — ${ticker}** — negative expected value | R/R: ${evRatio}x | Pipeline stopped. (${elapsedStr})`,
      });
    } else {
      this._enqueue({
        type: 'QUANT_COMPLETE',
        content: `📐 **Quant complete — ${ticker}**: ${recommendation} | R/R: ${evRatio}x | Size: ${sizePct}% (${elapsedStr})`,
      });
    }
  }

  riskComplete(ticker, decision, riskScore, blocked, reduced, elapsed) {
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    if (blocked) {
      this._enqueue({
        type: 'RISK_COMPLETE',
        content: `🛡️ ❌ **Risk BLOCKED ${ticker}** — Score: ${riskScore}/10 | Pipeline stopped. (${elapsedStr})`,
      });
    } else if (reduced) {
      this._enqueue({
        type: 'RISK_COMPLETE',
        content: `🛡️ **Risk reduced ${ticker}** — position size trimmed | Score: ${riskScore}/10 (${elapsedStr})`,
      });
    } else {
      this._enqueue({
        type: 'RISK_COMPLETE',
        content: `🛡️ **Risk approved ${ticker}** — Score: ${riskScore}/10 (${elapsedStr})`,
      });
    }
  }

  timingComplete(ticker, signal, earningsWarning, elapsed) {
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    const warn       = earningsWarning ? ' ⚠️ [EARNINGS WARNING]' : '';
    const icon       = signal === 'GO' ? '🎯' : signal === 'WAIT' ? '⏳' : '🔴';
    this._enqueue({
      type: 'TIMING_COMPLETE',
      content: `${icon} **Timing signal ${signal} — ${ticker}**${warn} (${elapsedStr})`,
    });
  }

  finalReport(ticker, verdict, filePath, elapsed) {
    const elapsedStr = elapsed ? `${(elapsed / 1000).toFixed(1)}s` : '—';
    const icon       = verdict === 'GO' ? '✅' : verdict === 'WAIT' ? '⏳' : verdict === 'BLOCKED' ? '🛑' : '⚠️';
    this._enqueue({
      type:    'FINAL_REPORT',
      content: `${icon} **Trade report — ${ticker}** | Signal: **${verdict}** | Elapsed: ${elapsedStr}`,
      filePath,
      fileName: `${ticker}-trade-report.md`,
    });
  }

  pipelineError(ticker, error) {
    this._enqueue({
      type:    'PIPELINE_ERROR',
      content: `🔥 **Trade pipeline error — ${ticker}**\n\`\`\`\n${String(error).slice(0, 400)}\n\`\`\``,
    });
  }

  // ── Queue management ────────────────────────────────────────────────────────

  _enqueue(notification) {
    this._queue.push(notification);
    if (!this._timer) this._scheduleNext();
  }

  _scheduleNext() {
    const now  = Date.now();
    const wait = Math.max(0, this._lastSent + RATE_LIMIT_MS - now);
    this._timer = setTimeout(() => this._flush(), wait);
  }

  async _flush() {
    this._timer = null;
    if (this._queue.length === 0) return;

    const n      = this._queue.shift();
    this._lastSent = Date.now();
    const target = this._resolveChannel(n.type);

    if (target) {
      try {
        if (n.filePath && fs.existsSync(n.filePath)) {
          const { AttachmentBuilder } = require('discord.js');
          const buf = fs.readFileSync(n.filePath);
          const att = new AttachmentBuilder(buf, { name: n.fileName || 'report.md' });
          await target.send({ content: n.content, files: [att] });
        } else {
          await target.send({ content: n.content });
        }
      } catch (err) {
        console.error(`[TradeFeed] Send error (${n.type} → ${target.name}):`, err.message);
        console.log(`[TradeFeed][${n.type}] ${n.content.replace(/\n/g, ' | ')}`);
      }
    } else {
      console.log(`[TradeFeed][${n.type}] ${n.content.replace(/\n/g, ' | ')}`);
    }

    if (this._queue.length > 0) this._scheduleNext();
  }
}

module.exports = TradeFeed;
