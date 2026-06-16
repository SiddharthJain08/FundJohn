#!/usr/bin/env bash
# Blueprint Fast Lane — OPERATOR-TRIGGERED bulk git import.
#
# Clean-room imports every already-coded strategy from the repos in
# src/ingestion/git_strategy_sources.json into research_candidates
# (origin='git_blueprint'): parse rule comment → Sonnet extractor → idempotent
# insert. The awesome-systematic-trading repo is ≈61 files, so a FULL run spends
# ≈$30 of Sonnet extractor calls (≈$0.50/file). The cloned code is read as TEXT
# only; it is never imported or executed.
#
# SAFE TO RE-RUN: the ingester skips any source_url already in research_candidates
# (existsFn dedup), so a crashed/interrupted run resumes for free — re-running
# only re-spends on files that never got inserted. There is no separate chunk
# bookkeeping; idempotency is the resume mechanism.
#
# nice -n 19 (2-core/8GB box; the extractor fan-out is light but keep it polite).
# Logs to $LOG. Runnable detached:
#   nohup scripts/bulk_git_ingest.sh /tmp/bulk_git_ingest.log >/dev/null 2>&1 &
#
# Env:
#   BULK_GIT_DRY_RUN=1   pass --dry-run (skips INSERT only; extractor STILL spends).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
export OPENCLAW_DIR="$ROOT"
# run_mastermind.js loads .env itself, but export POSTGRES_URI too so any
# standalone child (and a sanity print here) has it without sourcing .env
# (unquoted parens in .env break bash `source`).
export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' "$ROOT/.env" | cut -d= -f2- | tr -d '"'\''')

LOG="${1:-/tmp/bulk_git_ingest.log}"
DRY_FLAG=""
[ "${BULK_GIT_DRY_RUN:-0}" = "1" ] && DRY_FLAG="--dry-run"

CFG="$ROOT/src/ingestion/git_strategy_sources.json"
if [ ! -f "$CFG" ]; then
  echo "FATAL: missing $CFG" | tee -a "$LOG"; exit 2
fi

: > "$LOG"
echo "[$(date -u +%H:%M:%S)] bulk git-ingest START ${DRY_FLAG:-(live insert)}" | tee -a "$LOG"
echo "[$(date -u +%H:%M:%S)] config: $CFG" | tee -a "$LOG"

# The ingester loops over every repo + file internally and is itself idempotent,
# so this driver is a single nice'd, logged, re-runnable invocation.
if nice -n 19 node "$ROOT/src/agent/curators/run_mastermind.js" \
      --mode git-ingest $DRY_FLAG >>"$LOG" 2>&1; then
  rc=0
else
  rc=$?
fi

# Surface the JSON summary line the CLI prints on stdout (captured into $LOG).
SUMMARY=$(grep -E '"mode": "git-ingest"|"inserted"' "$LOG" | tail -1)
echo "[$(date -u +%H:%M:%S)] bulk git-ingest DONE rc=$rc ${SUMMARY}" | tee -a "$LOG"
exit "$rc"
