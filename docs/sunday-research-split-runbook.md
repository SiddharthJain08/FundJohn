# Sunday research split — activation runbook

Consolidates the **duplicate** weekend research runs into a single Sunday run,
segmented into an 08:00 ET *ingest* slot and a 14:00 ET *code+backtest* slot,
and fixes the Phase-4 `hunt` hang that timed out both runs on 2026-06-06/07.

## What changes

| Before | After |
|--------|-------|
| `openclaw-saturday-brain.timer` — Sat 10:00 ET — full 8-phase pipeline | **disabled** (duplicate) |
| `openclaw-weekend-sunday.timer` — Sun 08:00 ET — full 8-phase pipeline (SAME command) | **disabled**, replaced by the two below |
| — | `openclaw-sunday-research-ingest.timer` — Sun 08:00 ET — phases 0-4 |
| — | `openclaw-sunday-research-code.timer` — Sun 14:00 ET — phases 5-8 + candidate code-review |

Untouched: `openclaw-weekend-saturday` (Sat 08:00 portfolio adjustment/backtest),
`openclaw-strategy-review` (Sat 18:00), `openclaw-position-recs` (Sat 19:00),
`openclaw-universe-recs` (Sat 20:00), `openclaw-mastermind-critique`,
`mastermind-chat.service`.

## Pre-flip verification (no live change)

```bash
# 1. Tests + syntax
cd /root/openclaw && node --test tests/test_spawn_timeout.js tests/test_mastermind_code_review.js
node --check src/agent/curators/{saturday_brain,run_mastermind,saturday_brain_finisher,mastermind_code_review}.js

# 2. Ingest dry-run reaches hunt and stops (no 6h stall). EXPENSIVE-ish (Opus
#    expand+rate); run as claudebot so claude-bin auth + .env injection match prod:
sudo -u claudebot systemd-run --pipe --uid=claudebot \
  --property=EnvironmentFile=/root/openclaw/.env --working-directory=/root/openclaw \
  /usr/bin/node src/agent/curators/run_mastermind.js --mode saturday-brain --phase ingest --dry-run

# 3. Code-run dry-run: finisher tiers today's candidates without coding
sudo -u claudebot systemd-run --pipe --uid=claudebot \
  --property=EnvironmentFile=/root/openclaw/.env --working-directory=/root/openclaw \
  /usr/bin/node src/agent/curators/saturday_brain_finisher.js --dry-run
```

## Flip (operator)

```bash
# Disable the duplicate runs (keep unit files for rollback)
sudo systemctl disable --now openclaw-saturday-brain.timer
sudo systemctl disable --now openclaw-weekend-sunday.timer

# Install the two new Sunday units
sudo cp docs/sunday-research-ingest.service /etc/systemd/system/openclaw-sunday-research-ingest.service
sudo cp docs/sunday-research-ingest.timer   /etc/systemd/system/openclaw-sunday-research-ingest.timer
sudo cp docs/sunday-research-code.service   /etc/systemd/system/openclaw-sunday-research-code.service
sudo cp docs/sunday-research-code.timer     /etc/systemd/system/openclaw-sunday-research-code.timer
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-sunday-research-ingest.timer openclaw-sunday-research-code.timer

# Verify
systemctl list-timers | grep -E 'sunday-research|saturday-brain|weekend-sunday'
```

## Rollback

```bash
sudo systemctl disable --now openclaw-sunday-research-ingest.timer openclaw-sunday-research-code.timer
sudo systemctl enable  --now openclaw-saturday-brain.timer openclaw-weekend-sunday.timer
```

## Notes / follow-ups

- The 14:00 candidate code-review is **report-only** (writes
  `logs/code_review_candidates_sunday.md`). Gated auto-apply for candidates
  (apply → re-backtest → keep-if-non-regressing → revert) is the planned upgrade
  and must land + be reviewed before it edits candidate code automatically.
- A zero-new-candidate week leaves the ingest row at `status='ingest_complete'`
  with no code work — the re-pointed research-pipeline audit should treat
  `ingest_complete` as a healthy terminal state.
- Re-point `botjohn-saturday-maintenance` (Sat 16:00, audits research) and
  `botjohn-saturday-verify` (Sun 12:00) to fire AFTER the new Sunday cadence
  (e.g. audit Sun ~16:30 ET, verify Mon) so self-healing stays aligned.

## W4-5/W4-6: code-review split (2026-06-30)

### Why

`saturday_brain_finisher.js` was missing a `process.exit(0)` call after its final
async step. A `Type=oneshot` unit waits for the process to exit; without an explicit
exit the Node.js event loop kept open handles and hung indefinitely. systemd killed
it only when the 4h `TimeoutStartSec` expired — at which point the second `ExecStart`
(the Opus candidate code-review) never executed. The code-review **has not run since
2026-06-14** for this reason.

Fix W4-1 added `process.exit(0)` to the finisher. This split (W4-5) is
defense-in-depth: the Opus review now lives in its own unit with an independent 2h
timeout so it can never be starved by the finisher's budget regardless of hang
behaviour. The `ExecStartPre` zombie-reap guard (W4-6) also kills any lingering
finisher process before the next oneshot starts.

### New / changed units

| Tracked template | Installed as | Change |
|---|---|---|
| `docs/sunday-research-code.service` | `openclaw-sunday-research-code.service` | Finisher only (phases 5-8); code-review `ExecStart` removed; `ExecStartPre` reap guard added |
| `docs/sunday-code-review.service` | `openclaw-sunday-code-review.service` | NEW — Opus candidate review; `TimeoutStartSec=7200` |
| `docs/sunday-code-review.timer` | `openclaw-sunday-code-review.timer` | NEW — fires Sun 18:00 ET (after the 14:00 finisher slot) |

### Operator deploy (GATED — do not apply automatically)

```bash
# Re-copy the updated finisher unit (removes code-review ExecStart, adds W4-6 guard)
sudo cp docs/sunday-research-code.service \
    /etc/systemd/system/openclaw-sunday-research-code.service

# Install the new code-review unit + timer
sudo cp docs/sunday-code-review.service \
    /etc/systemd/system/openclaw-sunday-code-review.service
sudo cp docs/sunday-code-review.timer \
    /etc/systemd/system/openclaw-sunday-code-review.timer

sudo systemctl daemon-reload

# Enable + arm the new timer (will fire next Sun 18:00 ET)
sudo systemctl enable --now openclaw-sunday-code-review.timer

# W4-6: reap any lingering zombie processes from the old hung unit
sudo systemctl stop smoke-git-code.service smoke-git-code2.service

# Verify
systemctl list-timers | grep -E 'sunday-(research-code|code-review)'
systemctl status openclaw-sunday-code-review.timer
```
