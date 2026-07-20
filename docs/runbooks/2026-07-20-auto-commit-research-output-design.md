# Auto-commit research output — design

**Date:** 2026-07-20
**Status:** approved (operator), pending implementation
**Goal:** the weekend research pipeline's output should be committed + pushed to
GitHub automatically, so the system no longer requires a manual commit every week.

## Problem

The research pipeline (saturday-brain → `saturday_brain_finisher.js`) authors new
candidate strategies and updates the strategy registry, writing to:

- `src/strategies/implementations/*.py` + `*.requirements.json`
- `src/strategies/manifest.json`
- `src/strategies/registry.py`
- `src/strategies/strategy_signatures.json`

These changes accumulate **uncommitted** in the working tree. The commit was meant
to be LLM-agent-driven (the `run_maintenance.js` weekend prompts tell the agent to
"commit with `[botjohn-sunday]` and git push"), but that is unreliable and in
practice only covers the agent's own bug-fixes, not the research batch. Result:
weeks of research output sit uncommitted until a human runs `git commit` (or the
manual `!john /git sync` Discord command).

## Key constraint (decisive)

The research finisher runs as **`User=claudebot`** (`openclaw-sunday-research-code.service`).
`claudebot` **cannot commit or push**:

- `.git` is `root:root 755` → no group/other write → claudebot cannot write `.git`.
- claudebot has **no git identity** and **no SSH key**; `ssh -T git@github.com` →
  `Permission denied (publickey)`.

Only **root** can commit + push (root holds the deploy key; that's how the
root-scoped `johnbot`'s `!john /git sync` pushes). Therefore the commit must run
as **root**, triggered when the research service completes — NOT as an
`ExecStartPost` inside the claudebot service.

## Approach (chosen)

**systemd `OnSuccess=`** (systemd 257 supports it): the research finisher service,
on a clean exit, triggers a root-scoped commit service. This is event-driven (fires
exactly when research succeeds), and robust to the Sat/Sun timer swap because it
keys off the *service*, not a clock.

Rejected alternatives: a fixed-time root timer (the "scheduled sync" the operator
declined — could fire mid-run or long after); claudebot→johnbot signaling (more
moving parts).

## Components

### 1. `scripts/commit_research_output.sh` (new, runs as root)

Deterministic, no LLM. Steps:

1. `cd /root/openclaw`.
2. Uses the repo's already-resolvable git identity — verified: manual root commits
   (`8fd531a`, `02e7755`) resolve to the operator's identity. The script passes an
   explicit `-c user.name=… -c user.email=…` on the commit as a defensive fallback
   (a `botjohn-research` identity) so a stripped systemd env cannot fail the commit.
3. Stage **only** the research paths:
   `git add -A -- src/strategies/implementations src/strategies/manifest.json \
    src/strategies/registry.py src/strategies/strategy_signatures.json`
   — any other in-progress tree changes are left untouched.
4. If nothing staged → log "nothing to commit" and exit 0 (no empty commits).
5. Commit: `[botjohn-research] YYYY-MM-DD — auto research output (+N new / −M retired)`.
6. `git push origin main`. On rejection (remote ahead): `git pull --rebase` once,
   then retry the push. If it still fails: post a Discord alert and exit non-zero
   (never force-push).
7. Post a file summary to Discord via the existing webhook helper.

### 2. `openclaw-research-commit.service` (new)

`User=root`, `Type=oneshot`, `WorkingDirectory=/root/openclaw`,
`EnvironmentFile=/root/openclaw/.env`, `ExecStart=/root/openclaw/scripts/commit_research_output.sh`.
Added to `docs/systemd/` and the installer (`scripts/install_systemd.sh`).

### 3. Trigger

Add `OnSuccess=openclaw-research-commit.service` to
`openclaw-sunday-research-code.service` via a **drop-in** override
(`openclaw-sunday-research-code.service.d/onsuccess.conf`) — matches the repo's
existing `.service.d` pattern and leaves the base unit untouched. Snapshot the
drop-in in `docs/systemd/` too.

## Deliberate decisions

- **Only on success** — a failed/partial finisher does NOT auto-commit (`OnSuccess`
  fires only on exit 0); the operator gets the systemd failure signal instead.
  Half-baked research never lands.
- **Scoped staging** — structurally cannot repeat the "swept in unrelated work"
  mistake; only the 4 research paths are staged.
- **Commit + push** — root has SSH push; private repo, so a bad commit is easily
  fixed. Push-reject is handled by pull-rebase-retry, never force.
- **No test-gate** — research strategies are candidates, inert until the activation
  gates pass, so committing them is low-risk. Can be added later if desired.

## Out of scope

- The **current backlog** was committed manually as `8fd531a` (first application of
  the scoped-staging), not by this mechanism.
- Agent code-fixes (non-strategy edits) remain the weekend agent's responsibility
  (existing `[botjohn-sunday]` prompt flow), separate from this research commit.

## Validation

- `commit_research_output.sh` on a clean tree → exits 0, no commit.
- On a tree with only research-path changes → one `[botjohn-research]` commit +
  push; other paths untouched.
- With unrelated changes ALSO dirty → only research paths committed; unrelated
  changes remain dirty.
- `OnSuccess` wiring: after a manual `systemctl start openclaw-sunday-research-code`
  that exits 0, `openclaw-research-commit.service` runs.
