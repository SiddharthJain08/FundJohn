#!/usr/bin/env python3
"""
Wrapper: loads .env then runs pipeline_orchestrator.

Historical note (2026-04-21 parquet-primary migration): this script used to
run sync_master_parquets.py before the orchestrator because the engine read
from master parquets while the collector wrote to DB. Now the collector
writes directly to parquets so no sync step is needed.
"""
import sys, os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass

env = {**os.environ}
result = subprocess.run(
    [sys.executable, str(ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py')] + sys.argv[1:],
    cwd=str(ROOT), env=env,
)
sys.exit(result.returncode)
