'use strict';
/**
 * SP-6 Phase A — cron registration smoke test
 *
 * Checks:
 *  1. EOD gates ON → 5 EOD crons register without throwing; no close-exec crons.
 *  2. Both EOD + close-exec ON → throws mutual-exclusion error.
 *  3. All gates OFF → no EOD crons registered (gate-OFF inert).
 *
 * Runs in ~200ms (no real DB/Redis; event loop exits via process.exit).
 */

let passed = 0;
let failed = 0;

function assert(condition, label) {
    if (condition) {
        console.log(`  PASS  ${label}`);
        passed++;
    } else {
        console.error(`  FAIL  ${label}`);
        failed++;
    }
}

// ── Stub node-cron so registration is observable but never fires ──────────────
const registeredCrons = [];
require.cache[require.resolve('node-cron')] = {
    id:       require.resolve('node-cron'),
    filename: require.resolve('node-cron'),
    loaded:   true,
    exports: {
        schedule(expr, cb, opts) {
            registeredCrons.push({ expr, opts });
            return { stop: () => {} };
        },
        validate: () => true,
    },
};

// ── Stub pg Pool so module load doesn't need a real DB ────────────────────────
const pgMod = require('pg');
const _OrigPool = pgMod.Pool;
pgMod.Pool = class FakePool {
    query()  { return Promise.resolve({ rows: [] }); }
    end()    { return Promise.resolve(); }
};

// ── Stub Redis so module load doesn't need a real Redis ───────────────────────
try {
    const redisMod = require('../src/database/redis');
    // getClient() is called lazily inside callbacks — no stub needed for load.
} catch (_) {}

// ─────────────────────────────────────────────────────────────────────────────
// Test 1: EOD gates ON → 5 crons registered, no throw
// ─────────────────────────────────────────────────────────────────────────────
console.log('\nTest 1: EOD gates ON (all 5 crons)');
registeredCrons.length = 0;

process.env.OPENCLAW_EOD_SIGNAL_REGISTER = '1';
process.env.OPENCLAW_EOD_PREMARKET_GATE  = '1';
process.env.OPENCLAW_EOD_RECONCILE       = '1';
process.env.OPENCLAW_CLOSE_EXEC_LIVE     = '0';   // explicitly off

// Clear require cache so start() runs fresh
Object.keys(require.cache).forEach(k => {
    if (k.includes('cron-schedule')) delete require.cache[k];
});

let threwOnLoad = false;
try {
    const { start } = require('../src/engine/cron-schedule');
    // start() needs swarm + generateId + notifyDiscord stubs
    start(null, () => 'id', () => {});
} catch (e) {
    threwOnLoad = true;
    console.error('  Unexpected throw:', e.message);
}

assert(!threwOnLoad, 'No throw when EOD gates ON + close-exec OFF');

// Expected EOD cron expressions (UTC min/hour stored as ET in opts)
const eodExprs = registeredCrons.map(c => c.expr);
const nyTz     = registeredCrons.filter(c => c.opts && c.opts.timezone === 'America/New_York');

assert(eodExprs.includes('15 16 * * 1-5'), '(a) 16:15 ET EOD compute registered');
assert(eodExprs.includes('15 9 * * 1-5'),  '(b) 09:15 ET premarket gate registered');
assert(eodExprs.includes('25 9 * * 1-5'),  '(c) 09:25 ET open reconcile registered (moved pre-9:28 OPG cutoff 2026-06-08)');
assert(eodExprs.includes('32 9 * * 1-5'),  '(d) 09:32 ET OPG sweep registered');
assert(eodExprs.includes('55 15 * * 1-5'), '(e) 15:55 ET into-close fill registered');

// All EOD crons must use ET timezone
const eodExpected = ['15 16 * * 1-5', '15 9 * * 1-5', '25 9 * * 1-5', '32 9 * * 1-5', '55 15 * * 1-5'];
const allEtTz = eodExpected.every(expr =>
    registeredCrons.some(c => c.expr === expr && c.opts && c.opts.timezone === 'America/New_York')
);
assert(allEtTz, 'All 5 EOD crons use America/New_York timezone');

// Legacy 10am cron must NOT be registered
assert(!eodExprs.includes('0 10 * * 1-5'), '10am legacy cycle NOT registered when EOD=1');

// close-exec crons (3:10pm compute) must NOT be registered
assert(!eodExprs.includes('10 15 * * 1-5'), 'close-exec 3:10pm NOT registered when EOD=1');

// ─────────────────────────────────────────────────────────────────────────────
// Test 2: Both EOD + close-exec ON → mutual-exclusion throw
// ─────────────────────────────────────────────────────────────────────────────
console.log('\nTest 2: Mutual exclusion — EOD + close-exec both ON → must throw');
registeredCrons.length = 0;

process.env.OPENCLAW_EOD_SIGNAL_REGISTER = '1';
process.env.OPENCLAW_CLOSE_EXEC_LIVE     = '1';

Object.keys(require.cache).forEach(k => {
    if (k.includes('cron-schedule')) delete require.cache[k];
});

let threwMutualExclusion = false;
let meMsg = '';
try {
    const { start } = require('../src/engine/cron-schedule');
    start(null, () => 'id', () => {});
} catch (e) {
    threwMutualExclusion = true;
    meMsg = e.message;
}

assert(threwMutualExclusion, 'Throws when both EOD + close-exec ON');
assert(
    meMsg.includes('OPENCLAW_EOD_SIGNAL_REGISTER') && meMsg.includes('OPENCLAW_CLOSE_EXEC_LIVE'),
    'Error message names both conflicting gates'
);

// ─────────────────────────────────────────────────────────────────────────────
// Test 3: All gates OFF → no EOD crons registered
// ─────────────────────────────────────────────────────────────────────────────
console.log('\nTest 3: All gates OFF → EOD crons inert');
registeredCrons.length = 0;

process.env.OPENCLAW_EOD_SIGNAL_REGISTER = '0';
process.env.OPENCLAW_EOD_PREMARKET_GATE  = '0';
process.env.OPENCLAW_EOD_RECONCILE       = '0';
process.env.OPENCLAW_CLOSE_EXEC_LIVE     = '0';

Object.keys(require.cache).forEach(k => {
    if (k.includes('cron-schedule')) delete require.cache[k];
});

let threwOffLoad = false;
try {
    const { start } = require('../src/engine/cron-schedule');
    start(null, () => 'id', () => {});
} catch (e) {
    threwOffLoad = true;
    console.error('  Unexpected throw (gates OFF):', e.message);
}

assert(!threwOffLoad, 'No throw when all gates OFF');
const offExprs = registeredCrons.map(c => c.expr);
assert(!offExprs.includes('15 16 * * 1-5'), '(a) 16:15 NOT registered when gate OFF');
assert(!offExprs.includes('15 9 * * 1-5'),  '(b) 09:15 NOT registered when gate OFF');
assert(!offExprs.includes('25 9 * * 1-5'),  '(c) 09:25 NOT registered when gate OFF');
assert(!offExprs.includes('32 9 * * 1-5'),  '(d) 09:32 NOT registered when gate OFF');
assert(!offExprs.includes('55 15 * * 1-5'), '(e) 15:55 EOD NOT registered when gate OFF');
// Legacy 10am still registers when all gates off
assert(offExprs.includes('0 10 * * 1-5'), '10am legacy cycle registers when all gates OFF');

// ─────────────────────────────────────────────────────────────────────────────
console.log(`\n${'─'.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
    process.exit(1);
} else {
    console.log('All assertions passed.');
    process.exit(0);
}
