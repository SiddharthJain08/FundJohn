# systemd OnFailure drop-ins

`onfailure.conf` is installed as a drop-in under
`/etc/systemd/system/<unit>.service.d/onfailure.conf` for every openclaw
unit whose failure should reach Discord (currently 15 units).

The drop-in adds a single `OnFailure=` line that triggers
`openclaw-failure-notify@%n.service` (defined in
`../openclaw-failure-notify@.service`), which runs
`scripts/systemd_failure_notify.py <unit>` and posts a Discord notification
with the last 25 lines of journalctl for the failed unit.

Why a separate template rather than postFallback inside the script:
the 2026-05-13..18 silence was caused by a SyntaxError in
`src/agent/run_maintenance.js` at module-load time. The script's own
Discord-fallback (`postFallback()`) never ran because `main()` was
unreachable. systemd `OnFailure=` runs from outside the failing process,
so it survives module-load crashes, ENOMEM, ARG_MAX, and segfaults.

## Install

```sh
sudo mkdir -p /etc/systemd/system/<unit>.service.d
sudo cp onfailure.conf /etc/systemd/system/<unit>.service.d/
sudo cp ../openclaw-failure-notify@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Currently installed on:
- openclaw-botjohn-maintenance.service
- openclaw-botjohn-saturday-maintenance.service
- openclaw-botjohn-saturday-verify.service
- openclaw-strategy-review.service
- openclaw-position-recs.service
- openclaw-weekly-strategy-weights.service
- openclaw-saturday-brain.service
- openclaw-backtest-refresh.service
- openclaw-eod-refresh.service
- openclaw-mastermind-corpus.service
- openclaw-paper-expansion.service
- openclaw-phase2d-nightly.service
- openclaw-regime-live-pnl.service
- openclaw-strategy-backtest-refresh.service
- openclaw-tradable-universe-refresh.service

## Smoke-test

```sh
sudo systemctl start openclaw-failure-notify@openclaw-botjohn-maintenance.service
journalctl -u openclaw-failure-notify@openclaw-botjohn-maintenance.service -n 5
```

Should see `[failure-notify] posted N chars for openclaw-botjohn-maintenance`
in the journal and a 🚨 message in #botjohn-log.
