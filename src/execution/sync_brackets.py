"""src/execution/sync_brackets.py — thin pipeline-step wrapper.

The actual logic lives in scripts/sync_brackets.py so it can run
out-of-band from the operator shell. This module exists only so the
pipeline_orchestrator's _resolve_script convention (src/execution/<step>.py
or src/pipeline/<step>.py) can find it as a step entry-point.

Forwards argv to scripts/sync_brackets.main().
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    # Import inside main() so module-level import of execution.sync_brackets
    # (e.g. from tests) doesn't pull in the script's argparse + alpaca side
    # effects until invoked.
    from importlib import util
    spec = util.spec_from_file_location(
        'sync_brackets_script', str(ROOT / 'scripts' / 'sync_brackets.py'))
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(argv)


if __name__ == '__main__':
    sys.exit(main())
