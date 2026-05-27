"""SP-4 acceptance proof — originate ONE index-vol option strategy end-to-end,
candidate-only, with the gate ON. Surfaces the artifacts for operator review.
Does NOT promote, does NOT submit any order.

Usage (operator-approved only):
    OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1 \
        python3 scripts/sp4_origination_proof.py --candidate-id <uuid>

Pick a curated index-vol option paper already in research_candidates (e.g. a
SPY/QQQ short-straddle or variance-premium paper) and pass its candidate_id.
The harness fetches that candidate's real source_url from the DB so PaperHunter
hydrates the abstract, runs PaperHunter (Sonnet) → inspects inferred_*, and if
not rejected runs StrategyCoder (Sonnet). It then prints any non-equity manifest
entries for inspection. Nothing is promoted; no broker order is placed.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Drives the real orchestrator: fetch the candidate row → PaperHunter → (if not
# rejected) StrategyCoder. Prints HUNTER/REJECTED/CODED markers for the wrapper.
_NODE = (
    'const O = require("./src/agent/research/research-orchestrator");'
    ' const o = new O();'
    ' (async () => {'
    '   const cid = process.argv[1];'
    '   const { rows } = await o._query('
    '     "SELECT candidate_id, source_url, kind, hunter_result_json'
    '      FROM research_candidates WHERE candidate_id = $1", [cid]);'
    '   if (!rows.length) { console.error("candidate not found: " + cid); process.exit(2); }'
    '   const r = await o._runPaperHunter(rows[0]);'
    '   console.log("HUNTER " + JSON.stringify({'
    '     instrument_class: r.inferred_instrument_class,'
    '     underlying: r.inferred_option_underlying,'
    '     universe: r.inferred_universe_filter,'
    '     strategy_id: r.strategy_id, rejected: r.rejection_reason_if_any }));'
    '   if (r.rejection_reason_if_any) { console.log("REJECTED " + r.rejection_reason_if_any); process.exit(3); }'
    '   await o._codeStrategy(r);'
    '   console.log("CODED " + (r.strategy_id || ""));'
    '   process.exit(0);'
    ' })().catch(e => { console.error(e.message); process.exit(1); });'
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate-id', required=True)
    args = ap.parse_args()
    if os.environ.get('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT') != '1':
        sys.exit('Refusing: set OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1 to run the proof.')

    proc = subprocess.run(['node', '-e', _NODE, args.candidate_id], cwd=ROOT, check=False)

    # Print the manifest entries for any non-equity strategy (operator inspection).
    print('--- manifest entries with non-equity instrument_class ---')
    with open(os.path.join(ROOT, 'src/strategies/manifest.json')) as fh:
        mf = json.load(fh)
    for sid, e in (mf.get('strategies') or {}).items():
        if e.get('instrument_class', 'equity') != 'equity':
            print(sid, e.get('instrument_class'), e.get('state'))

    sys.exit(proc.returncode)


if __name__ == '__main__':
    main()
