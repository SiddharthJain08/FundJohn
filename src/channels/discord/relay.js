'use strict';

const MAX_LEN = 1990;

/**
 * Split text into chunks that fit within Discord's 2000-char limit.
 */
function split(text, maxLen = MAX_LEN) {
  const chunks = [];
  while (text.length > 0) {
    if (text.length <= maxLen) { chunks.push(text); break; }
    let at = text.lastIndexOf('\n', maxLen);
    if (at <= 0) at = maxLen;
    chunks.push(text.slice(0, at));
    text = text.slice(at).replace(/^\n/, '');
  }
  return chunks;
}

/**
 * Send a response to a Discord message, chunking if necessary.
 * @param {Message} message — original Discord message
 * @param {string} text — response text
 * @param {Object} opts — { channelOverride, ephemeral }
 */
async function send(message, text, opts = {}) {
  if (!text) return;
  const channel = opts.channelOverride || message.channel;
  const chunks = split(String(text));

  for (const chunk of chunks) {
    await channel.send({ content: chunk });
  }
}

/**
 * Format a diligence summary for Discord (<2000 chars).
 */
function formatDiligence(ticker, verdict, score, bullTarget, bearTarget, currentPrice) {
  const bullPct = currentPrice ? `(+${(((bullTarget - currentPrice) / currentPrice) * 100).toFixed(1)}%)` : '';
  const bearPct = currentPrice ? `(-${(((currentPrice - bearTarget) / currentPrice) * 100).toFixed(1)}%)` : '';

  const EMOJI = { PROCEED: '🟢', REVIEW: '🟡', KILL: '🔴' };
  return `🦞 **${ticker}** — **${verdict}** (${score}) ${EMOJI[verdict] || ''} | Bull: $${bullTarget} ${bullPct} | Bear: $${bearTarget} ${bearPct} | 📎 memo attached`;
}

/**
 * Format a trade summary for Discord (<2000 chars).
 */
function formatTrade(ticker, direction, entryLow, entryHigh, stop, target1, sizePct, signal) {
  return `📐 **${ticker} ${direction}** | Entry $${entryLow}-${entryHigh} | Stop $${stop} | T1 $${target1} | Size ${sizePct}% | Signal: **${signal}** | 📎 report attached`;
}

/**
 * Handle relay Discord commands.
 * Call from bot.js command router:
 *   await handleStrategistCommand(cmd, args, { workspace, relay, swarm, generateId })
 * Returns true if handled, false if unknown command.
 */
async function handleStrategistCommand(cmd, args, { workspace, relay, swarm, generateId }) {
    // Lazy-load agentPersonas to avoid circular dep — bot.js requires relay.js at top level
    const agentPersonas = (() => { try { return require('./agent-personas'); } catch { return null; } })();
    const rdPost   = (msg) => agentPersonas?.post('researchdesk', 'research-feed', msg).catch(() => {});
    const memoPost = (msg) => agentPersonas?.post('researchdesk', 'strategy-memos', msg).catch(() => {});
    const tdPost   = (msg) => agentPersonas?.post('tradedesk', 'trade-signals', msg).catch(() => {});
    const rpPost   = (msg) => agentPersonas?.post('tradedesk', 'trade-reports', msg).catch(() => {});

    switch (cmd) {
        case '/research reports': {
            const { Pool } = require('pg');
            const p        = new Pool({ connectionString: process.env.POSTGRES_URI });
            const reports  = await p.query(
                `SELECT sr.id, sh.name, sr.status, br.sharpe_ratio, br.annualized_return_pct, sr.created_at
                 FROM strategy_reports sr
                 JOIN strategy_hypotheses sh ON sr.hypothesis_id = sh.id
                 LEFT JOIN backtest_results br ON br.hypothesis_id = sh.id AND br.passed_validation = true
                 WHERE sr.workspace_id = $1 ORDER BY sr.created_at DESC LIMIT 10`,
                [workspace.id]
            );
            await relay.reply(
                `📊 **Strategy Reports**\n` +
                (reports.rows.length === 0 ? 'No reports yet.' :
                    reports.rows.map(r =>
                        `• **${r.name}** | ${r.status} | Sharpe: ${r.sharpe_ratio?.toFixed(2) || 'N/A'} | ${r.annualized_return_pct?.toFixed(1) || 'N/A'}%/yr`
                    ).join('\n'))
            );
            break;
        }
        case '/approve-dataset': {
            const name = args[1];
            if (!name) { await relay.reply('Usage: /approve-dataset {name}'); break; }
            const fs        = require('fs');
            const meta_path = `workspaces/default/data/master/${name}_meta.json`;
            if (!fs.existsSync(meta_path)) { await relay.reply(`Dataset ${name} not found.`); break; }
            const meta       = JSON.parse(fs.readFileSync(meta_path));
            meta.approved    = true;
            meta.approved_at = new Date().toISOString();
            meta.approved_by = 'operator';
            fs.writeFileSync(meta_path, JSON.stringify(meta, null, 2));
            await relay.reply(`✅ Dataset **${name}** approved for production use.`);
            break;
        }

        // ── Execution Engine Commands ──────────────────────────────────────
        case '/approve-strategy': {
            // /approve-strategy {strategy_id}
            const stratId = args[1];
            if (!stratId) { await relay.reply('Usage: /approve-strategy {strategy_id}'); break; }
            const { Pool: P2 } = require('pg');
            const p2 = new P2({ connectionString: process.env.POSTGRES_URI });
            const res = await p2.query(
                `UPDATE strategy_registry SET status='approved', approved_by='operator', approved_at=NOW()
                 WHERE id=$1 RETURNING id, name`,
                [stratId]
            );
            if (res.rows.length === 0) {
                await relay.reply(`Strategy \`${stratId}\` not found in registry.`);
            } else {
                await relay.reply(`✅ Strategy **${res.rows[0].name}** (\`${stratId}\`) approved and active.`);
            }
            await p2.end();
            break;
        }

        case '/strategy-review': {
            // List pending strategies
            const { Pool: P3 } = require('pg');
            const p3 = new P3({ connectionString: process.env.POSTGRES_URI });
            const pending = await p3.query(
                `SELECT id, name, tier, backtest_sharpe, backtest_return_pct, status, created_at
                 FROM strategy_registry WHERE status != 'deprecated' ORDER BY status, tier, created_at DESC`
            );
            await p3.end();
            if (pending.rows.length === 0) {
                await relay.reply('No strategies in registry.');
            } else {
                const lines = pending.rows.map(r =>
                    `• \`${r.id}\` **${r.name}** | T${r.tier} | ${r.status} | Sharpe: ${r.backtest_sharpe?.toFixed(2) || 'N/A'} | ${r.backtest_return_pct?.toFixed(1) || 'N/A'}%/yr`
                );
                await relay.reply(`**Strategy Registry**\n${lines.join('\n')}`);
            }
            break;
        }

        case '/signals': {
            // /signals [date] — show today's signals or a specific date
            const targetDate = args[1] || new Date().toISOString().slice(0, 10);
            const { Pool: P4 } = require('pg');
            const p4 = new P4({ connectionString: process.env.POSTGRES_URI });
            const sigs = await p4.query(
                `SELECT es.ticker, es.direction, es.strategy_id, es.position_size_pct,
                        es.entry_price, es.stop_loss, es.target_1, es.regime_state,
                        es.confidence, es.status
                 FROM execution_signals es
                 WHERE es.workspace_id=$1 AND es.signal_date=$2
                 ORDER BY es.strategy_id, es.ticker`,
                [workspace.id, targetDate]
            );
            await p4.end();
            if (sigs.rows.length === 0) {
                await relay.reply(`No signals for ${targetDate}.`);
            } else {
                const lines = sigs.rows.map(r =>
                    `• **${r.ticker}** ${r.direction} @ $${r.entry_price} | Stop: $${r.stop_loss} | T1: $${r.target_1} | ${(r.position_size_pct * 100).toFixed(1)}% | [${r.strategy_id}] | ${r.status}`
                );
                const body = `**Signals — ${targetDate}** (${sigs.rows.length} total)\n${lines.join('\n')}`;
                await relay.reply(body);
                // Mirror to #trade-signals via TradeDesk
                await tdPost(body);
            }
            break;
        }

        case '/engine-status': {
            const runner = require('../../execution/runner');
            const status = await runner.getLastRunStatus(workspace.id);
            if (!status) {
                await relay.reply('No engine run on record.');
            } else {
                await relay.reply(
                    `**Execution Engine Status**\n` +
                    `Status: ${status.status}\n` +
                    `Last run: ${status.run_date || 'unknown'}\n` +
                    `Signals: ${status.signals_generated ?? '—'} | Confluence: ${status.confluence_count ?? '—'}\n` +
                    `P&L updates: ${status.pnl_updates ?? '—'} | Triggers: ${status.report_triggers ?? '—'}\n` +
                    `Duration: ${status.duration_s ?? '—'}s`
                );
            }
            break;
        }

        case '/engine-run': {
            await relay.reply('⚙️ Running execution engine...');
            await tdPost('⚙️ **Execution engine running** — signals incoming');
            const workflow = require('../../agent/graph/workflow');
            try {
                const result = await workflow.runDailyClose(workspace.id);
                const runner = require('../../execution/runner');
                const report = runner.formatEngineReport(result);
                await relay.reply(report);
                if (result.signals_generated > 0 || result.report_triggers > 0) {
                    await tdPost(report);
                }
            } catch (err) {
                await relay.reply(`Engine error: ${err.message}`);
                await tdPost(`🚨 **Engine run failed**: ${err.message}`);
            }
            break;
        }

        case '/pause-strategy': {
            const stratId = args[1];
            if (!stratId) { await relay.reply('Usage: /pause-strategy {strategy_id}'); break; }
            const { Pool: P5 } = require('pg');
            const p5 = new P5({ connectionString: process.env.POSTGRES_URI });
            await p5.query(
                "UPDATE strategy_registry SET status='paused' WHERE id=$1",
                [stratId]
            );
            await p5.end();
            await relay.reply(`⏸️ Strategy **${stratId}** paused. Execution engine will skip it on next run. Resume with \`/approve-strategy ${stratId}\`.`);
            break;
        }

        // Adjust strategy parameters — creates a new versioned file
        case '/adjust-strategy': {
            // Usage: /adjust-strategy {base_id} {param}={value} {param}={value} reason: {why}
            // Example: /adjust-strategy MV01_momentum_value LONG_THRESHOLD=0.85 reason: tighten to top 15%

            const parts       = args.slice(1);
            const baseId      = parts[0];
            const reasonIdx   = parts.findIndex(p => p.toLowerCase().startsWith('reason:'));
            const reason      = reasonIdx >= 0 ? parts.slice(reasonIdx).join(' ').replace(/^reason:\s*/i, '') : 'Operator adjustment';
            const paramParts  = (reasonIdx >= 0 ? parts.slice(1, reasonIdx) : parts.slice(1));

            if (!baseId || paramParts.length === 0) {
                await relay.reply(
                    '📋 **Usage:** `/adjust-strategy {base_id} {param}={value} reason: {why}`\n\n' +
                    '**Example:**\n`/adjust-strategy MV01_momentum_value LONG_THRESHOLD=0.85 MIN_HISTORY=200 reason: tighten entry criteria`\n\n' +
                    'This creates a new versioned file (_v2, _v3, etc.) and activates it.\n' +
                    'The original file is never modified.'
                );
                break;
            }

            // Parse params
            const newParams = {};
            for (const part of paramParts) {
                const [key, val] = part.split('=');
                if (key && val !== undefined) {
                    // Try to parse as number, fall back to string
                    newParams[key.trim()] = isNaN(val) ? val.trim() : parseFloat(val);
                }
            }

            if (Object.keys(newParams).length === 0) {
                await relay.reply('No valid parameters parsed. Format: PARAM_NAME=value');
                break;
            }

            const versionMgr = require('../../engine/strategy-version-manager');
            try {
                const result = await versionMgr.createNewVersion(
                    workspace.id, baseId, newParams, reason
                );
                const changes = Object.entries(result.paramChanges)
                    .map(([k, v]) => `${k}: ${v.from} → ${v.to}`)
                    .join('\n  ');

                await relay.reply(
                    `✅ **Strategy versioned: ${result.versionedId}**\n\n` +
                    `Parameter changes:\n  ${changes}\n\n` +
                    `Reason: ${reason}\n` +
                    `Previous version: ${result.previousVersion} (deactivated)\n` +
                    `New file: ${result.newFilePath}\n\n` +
                    `Original file locked and preserved. New version active on next signal run.`
                );
            } catch(e) {
                await relay.reply(`❌ Versioning failed: ${e.message}`);
            }
            break;
        }

        // Show version history for a strategy
        case '/strategy-versions': {
            const baseId = args[1];
            if (!baseId) { await relay.reply('Usage: /strategy-versions {base_strategy_id}'); break; }

            const versionMgr = require('../../engine/strategy-version-manager');
            const history    = await versionMgr.getVersionHistory(workspace.id, baseId);

            if (history.length === 0) {
                await relay.reply(`No version history found for ${baseId}`);
                break;
            }

            const lines = history.map(v =>
                `${v.is_active ? '▶' : '◻'} **v${v.version}** — ${v.versioned_id}\n` +
                `  Deployed: ${new Date(v.deployed_at).toLocaleDateString()}\n` +
                (v.change_reason ? `  Reason: ${v.change_reason}\n` : '') +
                `  Signals: ${v.signal_count} | Validation: ${v.validation_passed ? '✅' : '❌'}`
            );

            await relay.reply(
                `📋 **Version History: ${baseId}**\n\n${lines.join('\n\n')}`
            );
            break;
        }

        // ── Data Agent — queue a collection task ──────────────────────────────
        case '/data': {
            // Usage: /data {description of what to collect}
            // Example: /data add 5 years of price history for NVDA and AMD
            const description = args.slice(1).join(' ').trim();
            if (!description) {
                await relay.reply(
                    '📋 **Usage:** `/data {description}`\n\n' +
                    '**Examples:**\n' +
                    '• `/data add 5 years of price history for NVDA and AMD`\n' +
                    '• `/data fetch options data for TSLA AAPL MSFT`\n' +
                    '• `/data collect insider transactions for all SP500 tickers`\n' +
                    '• `/data get macro data — GDP CPI rates`\n\n' +
                    'The data agent plans the collection. Results post to #data-alerts.'
                );
                break;
            }

            const { Pool: DPool } = require('pg');
            const dp = new DPool({ connectionString: process.env.POSTGRES_URI });

            // Create the data_tasks row
            const taskResult = await dp.query(
                `INSERT INTO data_tasks (workspace_id, description, status, requested_by)
                 VALUES ($1, $2, 'queued', $3) RETURNING id`,
                [workspace.id, description, relay.userId || 'operator']
            );
            await dp.end();

            const taskId = taskResult.rows[0].id;

            await relay.reply(
                `📋 **Data task queued** — \`${taskId.slice(0, 8)}\`\n` +
                `Request: *${description}*\n\n` +
                `Planning collection... results will post to #data-alerts.`
            );

            // Spawn the data-agent (Haiku) to build the plan
            try {
                await swarm.init({
                    type:      'data-agent',
                    mode:      'PLAN',
                    workspace,
                    threadId:  generateId(),
                    env: {
                        DATA_AGENT_TASK: description,
                        DATA_TASK_ID:    taskId,
                        WORKSPACE_ID:    workspace.id,
                    },
                    prompt: `DATA_AGENT_TASK: ${description}\nDATA_TASK_ID: ${taskId}`,
                });
            } catch (err) {
                await relay.reply(`⚠️ Data agent spawn failed: ${err.message}. Task \`${taskId.slice(0, 8)}\` is queued in DB — collection can be triggered manually.`);
            }
            break;
        }

        // ── Data task status ───────────────────────────────────────────────
        case '/data-status': {
            const { Pool: DS2 } = require('pg');
            const ds2 = new DS2({ connectionString: process.env.POSTGRES_URI });
            const tasks = await ds2.query(
                `SELECT id, description, status, rows_added, queued_at, completed_at
                 FROM data_tasks ORDER BY queued_at DESC LIMIT 10`
            );
            await ds2.end();

            if (tasks.rows.length === 0) {
                await relay.reply('No data tasks on record.');
                break;
            }

            const lines = tasks.rows.map(t =>
                `• \`${t.id.slice(0, 8)}\` **${t.status}** | +${t.rows_added ?? 0} rows | ${t.description.slice(0, 60)}${t.description.length > 60 ? '…' : ''}`
            );
            await relay.reply(`**Data Tasks (last 10)**\n${lines.join('\n')}`);
            break;
        }

        default:
            return false; // not handled by this router
    }
    return true;
}

module.exports = { send, split, formatDiligence, formatTrade, handleStrategistCommand };
