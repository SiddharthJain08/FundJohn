/**
 * JS twin of pipeline_orchestrator.py:_resolve_script.
 *
 * Step name → (argv, timeoutSec). Search order:
 *   1. src/pipeline/<step>.py  → python3 + --date,    600s
 *   2. src/pipeline/<step>.js  → node (no --date),   5400s
 *   3. src/execution/<step>.py → python3 + --date,    300s (720s for ic_gate_runner)
 *
 * PIPELINE_DRY_RUN=1 appends --dry-run to every step argv.
 * PIPELINE_ALPACA_DRY_RUN=1 appends --dry-run only to the alpaca_executor step.
 */
'use strict';

const fs   = require('node:fs');
const path = require('node:path');

const DEFAULT_ROOT = path.resolve(__dirname, '..', '..');

function resolveScript(step, runDate, env = process.env, root = DEFAULT_ROOT) {
  const fullDry  = env.PIPELINE_DRY_RUN === '1';
  const alpacaDry = env.PIPELINE_ALPACA_DRY_RUN === '1';

  const maybeDry = (argv) => {
    if (fullDry) argv.push('--dry-run');
    else if (alpacaDry && step === 'alpaca_executor') argv.push('--dry-run');
    return argv;
  };

  const pyPipe = path.join(root, 'src/pipeline', `${step}.py`);
  const jsPipe = path.join(root, 'src/pipeline', `${step}.js`);
  const pyExec = path.join(root, 'src/execution', `${step}.py`);

  if (fs.existsSync(pyPipe)) {
    return { argv: maybeDry(['python3', pyPipe, '--date', runDate]), timeoutSec: 600 };
  }
  if (fs.existsSync(jsPipe)) {
    // The collect step buffers up to 12k+ tickers' price rows before the
    // mid-flush checkpoint; raise the V8 heap limit so the Node process
    // does not get OOM-killed before it can flush. Its timeout is tunable
    // (OPENCLAW_COLLECT_TIMEOUT_SECONDS, same knob as the Python
    // orchestrator) — a healthy full-universe collect already takes ~85min
    // and rc=124 here kills the whole signal night (07-16, 07-20).
    const isCollect = step === 'run_collector_once';
    const nodeFlags = isCollect ? ['--max-old-space-size=4096'] : [];
    const timeoutSec = isCollect
      ? parseInt(env.OPENCLAW_COLLECT_TIMEOUT_SECONDS || '9000', 10)
      : 5400;
    return { argv: maybeDry(['node', ...nodeFlags, jsPipe]), timeoutSec };
  }
  if (fs.existsSync(pyExec)) {
    let timeoutSec = 300;
    if (step === 'ic_gate_runner') {
      timeoutSec = parseInt(env.IC_TIMEOUT_SECONDS || '600', 10) + 120;
    } else if (step === 'engine') {
      // The signals step. It was the ONLY pyExec step left on the bare 300s
      // literal, and it outgrew it: execution_runs shows duration climbing
      // 173s (07-21, 89 strategies) → 264s (08-04, 97), i.e. 88% of budget for
      // three days running, and on 2026-08-05 it blew through — rc=124 at
      // exactly 300128ms, ZERO signals for the day, no retry (there is none).
      // The creep is secular (strategy count + universe growth), NOT regime:
      // 07-31 TRANSITIONING ran 240/257s vs LOW_VOL 263/264s, and
      // strategies_run is 97 on every row regardless of regime.
      // 900 = ~3.4x the current mean. Keep in lockstep with the Python twin
      // in pipeline_orchestrator.py:_resolve_script — that path is what the
      // manual recovery invocation uses, so patching only this file leaves
      // recovery still capped at 300s.
      timeoutSec = parseInt(env.OPENCLAW_SIGNALS_TIMEOUT_SECONDS || '900', 10);
    }
    const execArgv = ['python3', pyExec, '--date', runDate];
    // The daily cycle runs during RTH, so the stop_reattach step places GTC
    // OCO exits (take-profit + stop) — Alpaca accepts OCO during RTH. The
    // 16:10 ET timer keeps running bare-stop-only as the overnight floor.
    // Default-ON; set OPENCLAW_STOP_REATTACH_OCO=0 to revert to stops-only.
    if (step === 'stop_reattach' && env.OPENCLAW_STOP_REATTACH_OCO !== '0') {
      execArgv.push('--oco');
    }
    return { argv: maybeDry(execArgv), timeoutSec };
  }
  throw new Error(`unknown step: ${step} (no script found at ${pyPipe}, ${jsPipe}, or ${pyExec})`);
}

module.exports = { resolveScript };
