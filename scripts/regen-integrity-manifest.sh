#!/usr/bin/env bash
# Regenerate the integrity manifest after intentional code changes.
#
# Run on the VPS after `git pull` whenever johnbot's startup log shows
# [SECURITY_ALERT] integrity: HASH MISMATCH for files you actually edited.
#
# Also run locally before committing if you want the new hashes in the
# same PR as the code change.
set -euo pipefail
cd "$(dirname "$0")/.."
npm run --silent integrity:generate
echo "Regenerated src/agent/config/integrity-manifest.json"
echo "Commit with: git add src/agent/config/integrity-manifest.json && git commit -m 'chore: regen integrity manifest'"
