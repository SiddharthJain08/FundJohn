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
    // does not get OOM-killed before it can flush.
    const nodeFlags = step === 'run_collector_once' ? ['--max-old-space-size=4096'] : [];
    return { argv: maybeDry(['node', ...nodeFlags, jsPipe]), timeoutSec: 5400 };
  }
  if (fs.existsSync(pyExec)) {
    let timeoutSec = 300;
    if (step === 'ic_gate_runner') {
      timeoutSec = parseInt(env.IC_TIMEOUT_SECONDS || '600', 10) + 120;
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
