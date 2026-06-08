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
