#!/usr/bin/env bash
# Auto-commit + push the weekend research pipeline's output.
#
# Runs as ROOT via openclaw-research-commit.service, triggered by
# `OnSuccess=` on openclaw-sunday-research-code.service. The research finisher
# runs as `claudebot`, which CANNOT write .git or push (no deploy key); only
# root can. Deterministic — no LLM. Scoped to the research-output paths ONLY,
# so unrelated in-progress tree changes are never swept in.
#
# Spec: docs/runbooks/2026-07-20-auto-commit-research-output-design.md
set -u
cd /root/openclaw || exit 1

LOG=/root/openclaw/logs/commit_research_output.log
log(){ echo "[research-commit $(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# The ONLY paths this commit ever touches.
PATHS=(
  src/strategies/implementations
  src/strategies/manifest.json
  src/strategies/registry.py
  src/strategies/strategy_signatures.json
)

# Automated commits are attributed to a clear bot identity (also serves as the
# defensive fallback so a stripped systemd env can never fail the commit on a
# missing user.name/email).
GIT_ID=(-c user.name=botjohn-research -c user.email=botjohn-research@fundjohn.local)

log "=== start ==="
git add -A -- "${PATHS[@]}"

if git diff --cached --quiet; then
  log "nothing to commit (no research-path changes) — exiting clean"
  exit 0
fi

STAT=$(git diff --cached --name-status)
N_ADD=$(printf '%s\n' "$STAT" | grep -cE '^A')
N_DEL=$(printf '%s\n' "$STAT" | grep -cE '^D')
N_MOD=$(printf '%s\n' "$STAT" | grep -cE '^M')
MSG="[botjohn-research] $(date -u +%F) — auto research output (${N_ADD} added / ${N_DEL} removed / ${N_MOD} modified)"

if ! git "${GIT_ID[@]}" commit -q -m "$MSG"; then
  log "commit FAILED"
  exit 1
fi
SHA=$(git rev-parse --short HEAD)
log "committed $SHA :: $MSG"

# Push. Handle a remote-ahead rejection once (pull --rebase then retry).
# NEVER force-push.
push_ok=0
if git push origin main >>"$LOG" 2>&1; then
  push_ok=1
else
  log "push rejected — attempting pull --rebase then retry"
  if git pull --rebase origin main >>"$LOG" 2>&1 && git push origin main >>"$LOG" 2>&1; then
    push_ok=1
  fi
fi
if (( push_ok )); then
  log "pushed $SHA to origin/main"
else
  log "PUSH FAILED after rebase-retry — $SHA is LOCAL ONLY; operator must push"
fi

# Discord summary — best-effort, time-bounded, never fails the run.
SUMMARY=$(printf '%s\n' "$STAT" | head -20)
timeout 30 node -e '
  const { postToChannel } = require("/root/openclaw/src/agent/curators/_discord_webhook");
  const [sha, pushed, body] = process.argv.slice(1);
  const head = pushed === "1"
    ? "✅ **Research auto-committed + pushed** (`" + sha + "`)"
    : "⚠️ **Research committed but PUSH FAILED** (`" + sha + "`) — needs a manual push";
  postToChannel("botjohn", "general", head + "\n```\n" + body + "\n```")
    .then(() => process.exit(0)).catch(() => process.exit(0));
' "$SHA" "$push_ok" "$SUMMARY" >>"$LOG" 2>&1 || log "discord summary skipped (non-fatal)"

log "=== done ==="
(( push_ok )) && exit 0 || exit 1
