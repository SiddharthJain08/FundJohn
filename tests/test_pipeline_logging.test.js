'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const MODULE_PATH = path.join(ROOT, 'src/execution/pipeline_logging.js');

// Stub the notifications module before requiring pipeline_logging
function makeStubbedLogger() {
  const calls = [];
  const stubNotifications = {
    post: async (channel, text) => { calls.push({ channel, text }); return true; },
  };
  // Inject via require.cache override
  const notifPath = require.resolve(path.join(ROOT, 'src/channels/discord/notifications.js'));
  require.cache[notifPath] = { id: notifPath, filename: notifPath, loaded: true, exports: stubNotifications };
  // Clear pipeline_logging cache so it re-requires the stub
  delete require.cache[require.resolve(MODULE_PATH)];
  const mod = require(MODULE_PATH);
  return { mod, calls };
}

test('feedStart posts ▶️ to #pipeline-feed', async () => {
  const { mod, calls } = makeStubbedLogger();
  await mod.feedStart('collect', '2026-05-21', 'scheduled');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].channel, 'pipeline-feed');
  assert.match(calls[0].text, /▶️.*collect.*2026-05-21/);
});

test('notifyFailure routes per STEP_FAILURE_CHANNEL', async () => {
  const { mod, calls } = makeStubbedLogger();
  // trade-half steps go to #trade-reports
  await mod.notifyFailure('trade', '2026-05-21', 2, 'sample stderr');
  // collect goes to #data-alerts
  await mod.notifyFailure('collect', '2026-05-21', 1, 'sample stderr');
  // health goes to #pipeline-feed (default)
  await mod.notifyFailure('health', '2026-05-21', 2, 'sample stderr');
  assert.equal(calls.length, 3);
  assert.equal(calls[0].channel, 'trade-reports');
  assert.equal(calls[1].channel, 'data-alerts');
  assert.equal(calls[2].channel, 'pipeline-feed');
});

test('webhook failure is non-fatal — logs warning, does not throw', async () => {
  const calls = [];
  const stubNotifications = {
    post: async () => { throw new Error('webhook down'); },
  };
  const notifPath = require.resolve(path.join(ROOT, 'src/channels/discord/notifications.js'));
  require.cache[notifPath] = { id: notifPath, filename: notifPath, loaded: true, exports: stubNotifications };
  delete require.cache[require.resolve(MODULE_PATH)];
  const mod = require(MODULE_PATH);
  // Should NOT throw
  await mod.feedStart('collect', '2026-05-21', 'scheduled');
  await mod.notifyFailure('trade', '2026-05-21', 2, 'oops');
});

test('STEP_FAILURE_CHANNEL and STEP_AGENTS maps are exported and complete', () => {
  const { mod } = makeStubbedLogger();
  const expectedSteps = [
    'collect', 'sentiment', 'signals', 'ic_gate', 'handoff',
    'trade', 'alpaca', 'reconcile', 'report',
    'pyportfolioopt_shadow', 'health',
  ];
  for (const step of expectedSteps) {
    assert.ok(mod.STEP_FAILURE_CHANNEL[step], `STEP_FAILURE_CHANNEL missing: ${step}`);
    assert.ok(mod.STEP_AGENTS[step],          `STEP_AGENTS missing: ${step}`);
  }
});
