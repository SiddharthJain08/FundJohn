// src/channels/api/pipelines_summary.js
// Pure tile summarizer. active/today/failures come from live traceBus runs
// ("since this process started" — labeled live_window). graphs is the UNION of
// live + durable (persisted) graph names so the panel isn't empty after a restart.
function graphOf(run) { return run?.meta?.graph || run?.meta?.graphName || run?.graph || 'unknown'; }

function summarizePipelines(liveRuns = [], persistedRuns = [], nowMs = Date.now()) {
  const live = liveRuns || [];
  const active = live.filter((r) => r.status === 'running').length;
  const failures_24h = live.filter((r) => {
    const isFail = r.status === 'error' || r.status === 'failed';
    return isFail && (nowMs - (r.updatedAt || 0) < 24 * 3600000);
  }).length;
  const todayStart = new Date(nowMs); todayStart.setHours(0, 0, 0, 0);
  const today = live.filter((r) => (r.startedAt || 0) >= todayStart.getTime()).length;
  const graphs = [...new Set([...live.map(graphOf), ...(persistedRuns || []).map(graphOf)])]
    .filter((g) => g && g !== 'unknown');
  return { active, today, failures_24h, graphs, live_window: 'since_restart' };
}
module.exports = { summarizePipelines, graphOf };
