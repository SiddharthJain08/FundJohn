# Options surface v3 (MFIV + RN density) and synthetic engine upgrades — rollout runbook

Spec: docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md · Plan: docs/superpowers/plans/2026-09-06-options-mfiv-rnd-synthetic-engine.md

## What changes when this merges
- `strategies.options_surface` is version **3**: seven new `SCALAR_KEYS` — `mfiv_30d`, `mfiv_90d`, `mf_tail_premium_30d`, `rn_skew_30d`, `rn_kurt_30d`, `rn_p_dn10_30d`, `rn_p_up10_30d`. Every v2 value is pinned unchanged by `tests/strategies/test_options_surface_v2_freeze.py`.
- Live: `engine.load_aux_data` computes the v3 keys on every cycle (flag `OPENCLAW_OPTIONS_SURFACE` unchanged — 0 = shadow, so the keys are summarised, not served, until the flag flips). Shadow line gains `mfiv_nonnull=…% rn_nonnull=…%`.
- Backtest: the enriched panel carries the v3 columns after the rebuild below; `aux_data_loader.FIELDS` exposes them. No manifest strategy reads them yet.
- Synthetic options engine (`backtest.options_backtest`, no manifest consumer): dividend yield `q` from `corporate_actions.parquet`, American exercise (CRR tree) by default (`OptionSpec.exercise`), IV from the real surface master when it covers the date, else the VIX9D/VIX term point, else realized × VRP. One `[options_backtest] iv sources:` line per run.

## Steps (controller, after merge, first idle window — never beside a fleet child)
1. `sudo systemd-run --unit=surface-v3-rollout-$(date -u +%Y%m%d) -p Nice=19 -p MemoryMax=3500M -p RuntimeMaxSec=5h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src --working-directory=/root/openclaw /bin/bash scripts/rollout_surface_v3.sh` — waits for `openclaw-fleet-overnight-resume` / `fleet-rf-epoch-20260906` / `options-surface-rollout-20260906` to be inactive, rebuilds `data/master/options_surface.parquet` from 2026-06-29 (append_dedup replace on (ticker, date) — UNION BY NAME fills the new columns for every rebuilt row), rebuilds `data/derived/options_aggregates_enriched.parquet`, verifies.
2. Verification thresholds (script exit 0 = all met): `mfiv_nonnull ≥ 90 %` and `rn_nonnull ≥ 90 %` of tickers with ≥ 2 fitted expiries on the latest session; every latest-session row at version 3; SPY `0 ≤ mf_tail_premium_30d ≤ 0.03`, `rn_skew_30d < 0`, `0.1 % ≤ rn_p_dn10_30d ≤ 10 %`; the panel carries all seven columns.
3. No re-backtest: no strategy reads a v3 key and no manifest strategy uses the synthetic engine. The options sleeve's v2 values are byte-identical (freeze test).
4. Results: **TO BE FILLED by the rollout run on main** (surface rows/dates, coverage percentages, SPY row, panel rows).

## Watch list
- First live shadow line after merge: `mfiv_nonnull` / `rn_nonnull` ≥ 80 % (live chains are thinner than the EOD master's — 90 % is the master's bar).
- `python3 -m system_checks --check options_aux_freshness` unchanged.
- `scripts/options_parity_check.py`'s IV gate is near-zero on the surface overlap by construction (ruling G8); measure the vix_term tier with `OPENCLAW_OPTIONS_SURFACE_PATH` pointed at an empty path.

## Rollback
- Surface: revert the Part A commits, then `scripts/compute_rolling_options_fields.py` (the panel is derived; the master's extra v3 columns are harmless to v2 readers — never delete a master).
- Engine: `git revert` of the Part B commits; nothing live depends on it.
