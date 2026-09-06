#!/usr/bin/env bash
# scripts/rollout_surface_v3.sh — options surface v3 rebuild + panel rebuild + verification
# (spec docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md §C).
# Run as a transient unit in an idle window; it waits for any fleet child first:
#   sudo systemd-run --unit=surface-v3-rollout-$(date -u +%Y%m%d) -p Nice=19 -p MemoryMax=3500M \
#     -p RuntimeMaxSec=5h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src \
#     --working-directory=/root/openclaw /bin/bash scripts/rollout_surface_v3.sh
#   scripts/rollout_surface_v3.sh --verify-only     # re-run the checks on the current masters
set -uo pipefail
cd /root/openclaw || exit 2
export PYTHONPATH=/root/openclaw/src
VERIFY_ONLY=0; START=2026-06-29; END=$(date -u +%F)
while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) VERIFY_ONLY=1;; --start) START="$2"; shift;; --end) END="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
ts() { date -u +%FT%TZ; }
if [ "$VERIFY_ONLY" = 0 ]; then
  for u in openclaw-fleet-overnight-resume.service fleet-rf-epoch-20260906.service options-surface-rollout-20260906.service; do
    while systemctl is-active --quiet "$u"; do echo "[v3 $(ts)] waiting for $u"; sleep 300; done
  done
  echo "[v3 $(ts)] build $START..$END"
  python3 scripts/build_options_surface.py --start "$START" --end "$END" || { echo "[v3 $(ts)] build FAILED"; exit 1; }
  echo "[v3 $(ts)] panel rebuild"
  python3 scripts/compute_rolling_options_fields.py || { echo "[v3 $(ts)] panel FAILED"; exit 1; }
fi
echo "[v3 $(ts)] verify"
python3 - <<'PY'
import sys
import pyarrow.parquet as pq, pandas as pd
cols = ['ticker', 'date', 'iv30', 'n_expiries_fit', 'options_features_version', 'mfiv_30d', 'mfiv_90d',
        'mf_tail_premium_30d', 'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d', 'iv30_source']
meta = pq.read_metadata('data/master/options_surface.parquet')
last = pq.read_table('data/master/options_surface.parquet', columns=['date']).to_pandas()['date'].max()
df = pq.read_table('data/master/options_surface.parquet', columns=cols,
                   filters=[('date', '==', last)]).to_pandas()
fit = df[df['n_expiries_fit'] >= 2]
mf = fit['mfiv_30d'].notna().mean() * 100; rn = fit['rn_skew_30d'].notna().mean() * 100
ver = df['options_features_version'].value_counts().to_dict()
spy = df[df['ticker'] == 'SPY'].iloc[0] if (df['ticker'] == 'SPY').any() else None
print(f'surface rows={meta.num_rows:,} latest={pd.Timestamp(last).date()} tickers={df.ticker.nunique():,} '
      f'fit>=2: {len(fit):,} mfiv_nonnull={mf:.1f}% rn_nonnull={rn:.1f}% version={ver}')
src = df['iv30_source'].value_counts(dropna=False).to_dict()
print(f'iv30 nonnull={df.iv30.notna().mean()*100:.1f}% of {len(df):,} tickers; iv30_source={src}')
ok = mf >= 90 and rn >= 90 and ver.get(3, 0) == len(df)
if spy is None:
    print('SPY missing from the latest session')
    ok = False
else:
    print('SPY', {k: (None if pd.isna(spy[k]) else round(float(spy[k]), 4)) for k in cols[2:] if k != 'iv30_source'})
    ok &= 0.0 <= float(spy['mf_tail_premium_30d']) <= 0.03 and float(spy['rn_skew_30d']) < 0 \
          and 0.001 <= float(spy['rn_p_dn10_30d']) <= 0.10
panel = pq.read_metadata('data/derived/options_aggregates_enriched.parquet')
pcols = set(pq.read_schema('data/derived/options_aggregates_enriched.parquet').names)
missing = [c for c in cols[5:] if c not in pcols]
print(f'panel rows={panel.num_rows:,} v3 columns missing={missing}')
ok &= not missing
print('VERIFY', 'OK' if ok else 'FAIL')
sys.exit(0 if ok else 1)
PY
rc=$?
echo "[v3 $(ts)] end rc=$rc"
exit $rc
