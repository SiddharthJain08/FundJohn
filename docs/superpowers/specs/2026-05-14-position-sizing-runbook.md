# Position-sizing rewrite — Operator runbook

**Status:** All 12 plan tasks shipped to `main`, flags OFF. This doc has the exact commands to flip the new sizer on, plus the rollback path.

## What changed (summary)

- New per-regime weights table `strategy_weights_by_regime` (currently populated: 19 LOW_VOL / 32 TRANSITIONING / 1 CRISIS strategies, Σ weight = 1.0 each).
- New endpoint `/api/portfolio/ticker-alpha/:ticker` was already in place; **new** endpoints `GET/PUT /api/config/lambda` for the dashboard slider.
- New sizer path `_sharpe_cadence_path` in `src/execution/regime_blended_sizer.py`, gated by `OPENCLAW_SHARPE_CADENCE_SIZER=1`. **Default OFF.**
- New module `src/execution/strategy_weights.py` with `--rebuild`, `--show-negative` CLI.
- TradeJohn narrowed to keep|cancel (effective on the next sizer call regardless of which path).
- New cron: `openclaw-weekly-strategy-weights.{service,timer}` installed at `/etc/systemd/system/` but **disabled**.
- New lifecycle path `live → candidate` (and `monitoring → candidate`) for auto-demote.

## Pre-flip checklist

- [x] `tools/verify_sizing.js` — all PASS
- [x] `tests/test_sharpe_blend.py` — 6/6 PASS
- [x] `tests/test_force_fire.py` — 1/1 PASS
- [x] `python3 -m system_checks` — 0 FAIL/ERROR
- [x] Lambda slider visible at http://localhost:3000/ Portfolio tab
- [ ] **Operator review** — confirm the 24 candidate auto-demotions look reasonable:
  ```bash
  cd /root/openclaw
  export POSTGRES_URI=$(grep ^POSTGRES_URI .env | cut -d= -f2- | tr -d '"')
  PYTHONPATH=src python3 -m execution.strategy_weights --show-negative
  ```

## Flip steps

### 1. Enable the new sizer flag

```bash
# Append to /root/openclaw/.env (so johnbot.service picks it up via EnvironmentFile)
echo 'OPENCLAW_SHARPE_CADENCE_SIZER=1' >> /root/openclaw/.env

# Restart so the bot reads the new env
systemctl restart johnbot.service
sleep 2 && systemctl is-active johnbot.service
```

Verify the env shows up:
```bash
systemctl show johnbot.service -p Environment | grep SHARPE_CADENCE
# Note: EnvironmentFile entries do NOT show in systemctl show; verify by
# checking the running process:
ps eww -p $(pgrep -f 'src/channels/discord/bot.js' | head -1) | tr ' ' '\n' | grep SHARPE_CADENCE
```

### 2. (Optional) Enable opt-in auto-demote chain

If you want the weekly cron + manual rebuild to actually demote the 24 universally-negative strategies (rather than just printing them):
```bash
echo 'OPENCLAW_AUTO_DEMOTE=1' >> /root/openclaw/.env
```

(Skip this if you want to keep the dry-run state — the operator can still demote manually via `python3 -m strategies.demote --strategy_id ...` or the lifecycle CLI.)

### 3. Enable the weekly cron

```bash
systemctl enable --now openclaw-weekly-strategy-weights.timer
systemctl list-timers | grep weekly-strategy-weights
```

Expected: timer listed, next firing on the upcoming Sunday at 10:00 UTC.

### 4. First pipeline-cycle verification

After the next 10:00 ET cycle finishes:

```bash
cd /root/openclaw
export POSTGRES_URI=$(grep ^POSTGRES_URI .env | cut -d= -f2- | tr -d '"')
node tools/verify_sizing.js
```

The lambda invariant section should print PASS (was previously skipped because no sized-handoff existed yet).

## Rollback

If anything goes wrong:

```bash
# Disable the new sizer (instant; takes effect on the next pipeline tick)
sed -i '/^OPENCLAW_SHARPE_CADENCE_SIZER=/d' /root/openclaw/.env
sed -i '/^OPENCLAW_AUTO_DEMOTE=/d' /root/openclaw/.env
systemctl restart johnbot.service

# Disable the weekly cron
systemctl disable --now openclaw-weekly-strategy-weights.timer
```

The old `_consolidate_path` / `_independent_path` resume immediately. No schema rollback needed (new table is additive).

## Dashboard

- Portfolio page header now has a `λ` slider between the heatmap info line and the "Show all" button. Drag → debounced 400 ms → PUT `/api/config/lambda` → next pipeline cycle reads the new value.
- Range: 0.10× to 3.50× NAV. Default 2.0×.
- The sizer reads from `pipeline_config.position_sizing_lambda` on every cycle, so a slider change applies on the very next 10:00 ET tick.

## Commits shipped (main branch)

| commit | scope |
|---|---|
| migrations 090 + 091 | schema + lambda seed |
| `cadence.py`, `strategy_weights.py`, `test_sharpe_blend.py` | weights engine + blend test |
| `signal_cadence_gate.py`, `regime_liquidator.py`, `test_force_fire.py` | force_all + Redis trigger |
| `regime_blended_sizer.py` | _sharpe_cadence_path behind flag |
| `tradejohn_confirmer.py` + prompt | narrow to keep\|cancel |
| `lifecycle.py` + opt-in auto-demote chain | demote on universal negative Sharpe |
| `server.js` lambda API + slider | `/api/config/lambda` + Portfolio UI |
| `tools/verify_sizing.js` | math-invariant probe |
| `weekly_live_sharpe.js` + systemd units | Sunday 06:00 ET cron |

All on `main`, pushed to origin.
