# Weekend schedule migration (2026-05-29)

Replaces the scattered weekend timers with four units. Old units are
`systemctl disable --now`'d (NOT deleted) so they can be re-enabled if needed.

## New units (install + enable at deploy)
| Unit | When (ET) | Runs |
|------|-----------|------|
| openclaw-weekend-saturday | Sat 08:00 | weekend_saturday.sh (review->critique->position-recs->coupling->backtest refresh->weights->panels->universe-recs) |
| openclaw-weekend-maintenance-sat | Sat 20:00 | run_maintenance.js --mode weekend-sat |
| openclaw-weekend-sunday | Sun 08:00 | run_mastermind.js --mode saturday-brain (research) |
| openclaw-weekend-maintenance-sun | Sun 20:00 | run_maintenance.js --mode weekend-sun (audits research) |

## Disabled (superseded; folded into the new units)
openclaw-mastermind-corpus, openclaw-paper-expansion, openclaw-backtest-refresh,
openclaw-strategy-backtest-refresh, openclaw-weekly-strategy-weights,
openclaw-strategy-review, openclaw-mastermind-critique, openclaw-position-recs,
openclaw-universe-recs, openclaw-botjohn-saturday-maintenance,
openclaw-botjohn-saturday-verify.

## Deploy commands (run on VPS)
    for u in openclaw-weekend-saturday openclaw-weekend-maintenance-sat \
             openclaw-weekend-sunday openclaw-weekend-maintenance-sun; do
      sudo cp docs/$u.service docs/$u.timer /etc/systemd/system/
    done
    sudo systemctl daemon-reload
    for u in openclaw-weekend-saturday openclaw-weekend-maintenance-sat \
             openclaw-weekend-sunday openclaw-weekend-maintenance-sun; do
      sudo systemctl enable --now $u.timer
    done
    for u in mastermind-corpus paper-expansion backtest-refresh \
             strategy-backtest-refresh weekly-strategy-weights strategy-review \
             mastermind-critique position-recs universe-recs \
             botjohn-saturday-maintenance botjohn-saturday-verify; do
      sudo systemctl disable --now openclaw-$u.timer || true
    done
