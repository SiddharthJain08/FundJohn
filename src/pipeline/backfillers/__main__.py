"""Allow `python3 -m src.pipeline.backfillers <request_id>` to invoke the
canonical backfill entry. Used by src/lib/backfill_runner.js (the Node
wrapper called from the fused staging-approval worker) and by ad-hoc
operator runs.
"""
import sys

from src.pipeline.backfillers import _cli

if __name__ == '__main__':
    sys.exit(_cli())
