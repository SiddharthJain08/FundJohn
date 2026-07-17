# Corpus Curator — Weekly Schedule

Phase 2 of the Opus Corpus Curator runs automatically every **Saturday at 10:00
America/New_York** (EST/EDT — the unit adjusts for DST).

The curator (1) discovers new papers into `research_corpus`, (2) runs the Opus
curator on any un-curated papers, and (3) promotes high-confidence picks into
`research_candidates` so the next `/research start` picks them up.

## Install (run once as root)

```bash
# Unit files live under /etc/systemd/system/ on this host.
sudo cp /root/openclaw/docs/curator.service /etc/systemd/system/openclaw-curator.service
sudo cp /root/openclaw/docs/curator.timer   /etc/systemd/system/openclaw-curator.timer
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-curator.timer
```

## Verify

```bash
systemctl list-timers openclaw-curator.timer      # shows next fire time
systemctl status openclaw-curator.service         # last run status
journalctl -u openclaw-curator.service -n 100     # last run logs
```

## Manual trigger (for testing)

```bash
sudo systemctl start openclaw-curator.service
```

## Costs

- Per paper: ~$0.02–0.04 (Opus 4.7, low effort)
- Per weekly sweep of ~500 papers: ~$10–20
- Hard per-run cap enforced by `maxBudgetUsd: 8.0` per batch in
  `src/agent/config/subagent-types.json`

## Rollback

```bash
sudo systemctl disable --now openclaw-curator.timer
sudo rm /etc/systemd/system/openclaw-curator.{service,timer}
sudo systemctl daemon-reload
```
