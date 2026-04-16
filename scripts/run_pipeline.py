#!/usr/bin/env python3
"""Wrapper: loads .env then runs pipeline_orchestrator with correct env."""
import sys, os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Load .env via python-dotenv (handles special chars safely)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass

env = {**os.environ}
result = subprocess.run(
    [sys.executable, str(ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py')] + sys.argv[1:],
    cwd=str(ROOT), env=env
)
sys.exit(result.returncode)
