#!/usr/bin/env python3
"""Phase 1G — orchestrator-callable wrapper for the PyPortfolioOpt
HRP shadow alt-sizer.

The pipeline_orchestrator dispatches each step as
  python3 src/execution/<step>.py --date YYYY-MM-DD
This module is a thin wrapper that delegates to scripts/run_pyportfolioopt_shadow.py
so the canonical CLI entry stays under scripts/ (per the plan), while the
orchestrator can find and invoke a same-named module under src/execution/.

The shadow sizer self-gates on OPENCLAW_PYPORTFOLIOOPT_SHADOW=1; with the
gate unset this exits 0 with a 'skipping' message so the orchestrator
treats it as a successful no-op.

NEVER routes to broker.  Pure read of handoff + sized.json + parquet,
write to pyportfolioopt_shadow_runs only.
"""
from __future__ import annotations

import argparse
import runpy
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SHADOW_SCRIPT = ROOT / "scripts" / "run_pyportfolioopt_shadow.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="PyPortfolioOpt HRP shadow-sizer wrapper")
    parser.add_argument("--date", required=False)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="PIPELINE_DRY_RUN passthrough — no DB write, just prints intent.")
    args, _ = parser.parse_known_args()

    if args.dry_run:
        print(f"[pyportfolioopt_shadow] --dry-run; would run shadow sizer for {args.date or 'today'}")
        return 0

    # Build argv for the underlying script and exec it in this process.
    argv = [str(SHADOW_SCRIPT)]
    if args.date:
        argv += ["--date", args.date]
    if args.force:
        argv.append("--force")
    sys.argv = argv
    # CRITICAL: this wrapper must never abort the cycle. The shadow sizer is
    # a non-production sidecar; any crash (psycopg2 failure, malformed handoff,
    # ValueError, etc.) must not kill the next orchestrator step or skip
    # mark_completed().
    try:
        runpy.run_path(str(SHADOW_SCRIPT), run_name="__main__")
    except SystemExit as e:
        # Preserve the script's own exit code (e.g. its main() returns 0 even
        # on skip-paths; an explicit sys.exit(2) would still propagate).
        return int(e.code or 0)
    except Exception as exc:
        print(f"[PyPortfolioOpt-shadow] WARN: shadow sizer failed (non-fatal): {exc!s}",
              file=sys.stderr)
        traceback.print_exc()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
