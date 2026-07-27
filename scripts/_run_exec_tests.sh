#!/bin/bash
# Durable CHUNKED runner for tests/execution.
# The directory in ONE pytest process peaks at 5.6G and gets OOM-killed on this
# 8GB box (~78% through), so each test FILE runs as its own process and the
# interpreter releases memory between files. POSTGRES_URI comes from the unit's
# EnvironmentFile=.env.
cd /root/openclaw || exit 1
OUT="$1"
: > "$OUT"
for f in tests/execution/test_*.py; do
  PYTHONPATH=src nice -n 10 python3 -m pytest "$f" -q --tb=no -rf >> "$OUT" 2>&1
  echo "___FILE_DONE rc=$? $f" >> "$OUT"
done
echo "ALL_CHUNKS_DONE" >> "$OUT"
