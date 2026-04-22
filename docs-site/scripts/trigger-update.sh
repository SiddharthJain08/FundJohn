#!/usr/bin/env bash
# Trigger a Mintlify deploy for this project.
#
# Requires both MINTLIFY_PROJECT_ID and MINTLIFY_ADMIN_KEY in /root/openclaw/.env.
# The admin key is the `mint_`-prefixed server-side secret from the Mintlify
# dashboard → Settings → API Keys. The project ID is the slug/uuid you get
# when creating the project.
#
# Usage: ./scripts/trigger-update.sh
set -euo pipefail

ENV_FILE="${ENV_FILE:-/root/openclaw/.env}"
[[ -f "$ENV_FILE" ]] || { echo "error: $ENV_FILE not found" >&2; exit 1; }

# shellcheck disable=SC1090
source <(grep -E '^MINTLIFY_(PROJECT_ID|ADMIN_KEY)=' "$ENV_FILE")

if [[ -z "${MINTLIFY_PROJECT_ID:-}" ]]; then
  echo "error: MINTLIFY_PROJECT_ID missing in $ENV_FILE" >&2
  exit 1
fi
if [[ -z "${MINTLIFY_ADMIN_KEY:-}" ]]; then
  echo "error: MINTLIFY_ADMIN_KEY missing in $ENV_FILE (get it from dashboard → Settings → API Keys, prefixed 'mint_')" >&2
  exit 1
fi

echo "→ triggering Mintlify deploy for project ${MINTLIFY_PROJECT_ID}..."
curl -sS -X POST \
  -H "Authorization: Bearer ${MINTLIFY_ADMIN_KEY}" \
  "https://api.mintlify.com/v1/project/update/${MINTLIFY_PROJECT_ID}" \
  -w "\nhttp_status=%{http_code}\n"
