#!/usr/bin/env bash
# deploy_top10.sh
# ===============
# One-shot deploy of the Top-10 FundJohn strategy cohort to the VPS.
#
#   Usage:
#     ./deploy_top10.sh [--shadow | --paper | --live]   (default: --shadow)
#     ./deploy_top10.sh --dry-run
#
# Phases (in order):
#   1. sanity: verify VPS paths + required parquet files
#   2. install: copy implementations/*.py → /root/openclaw/src/strategies/implementations/
#   3. install: copy engine_patches/aux_metrics.py → /root/openclaw/src/engine_patches/
#   4. install: copy ingest/*.py → /root/openclaw/src/ingest/
#   5. register: patch /root/openclaw/src/strategies/registry.json
#   6. set phase: shadow → paper → live in /root/openclaw/config/strategy_phases.json
#   7. smoke test: import each strategy and call generate_signals({}, {})
#   8. echo a deploy receipt
#
# Safe-by-default: the deploy target is SHADOW unless you pass --paper or --live.
# Shadow mode → strategies run and log signals but do NOT size / fire orders.
#
# Author: Claude / FundJohn research, 2026-04-23.

set -euo pipefail

# ── CONFIG ───────────────────────────────────────────────────────────────────
OPENCLAW_ROOT="${OPENCLAW_ROOT:-/root/openclaw}"
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"         # top10_strategies_2026/
IMPL_DST="$OPENCLAW_ROOT/src/strategies/implementations"
PATCH_DST="$OPENCLAW_ROOT/src/engine_patches"
INGEST_DST="$OPENCLAW_ROOT/src/ingest"
REGISTRY="$OPENCLAW_ROOT/src/strategies/registry.json"
PHASE_CFG="$OPENCLAW_ROOT/config/strategy_phases.json"

STRATEGIES=(
  "shv13_call_put_iv_spread.py:S_HV13_call_put_iv_spread:CallPutIVSpread"
  "shv14_otm_skew_factor.py:S_HV14_otm_skew_factor:OTMSkewFactor"
  "shv15_iv_term_structure.py:S_HV15_iv_term_structure:IVTermStructureSlope"
  "shv17_earnings_straddle_fade.py:S_HV17_earnings_straddle_fade:EarningsStraddleFade"
  "shv20_iv_dispersion_reversion.py:S_HV20_iv_dispersion_reversion:IVDispersionReversion"
  "str01_vvix_early_warning.py:S_TR01_vvix_early_warning:VVIXEarlyWarning"
  "str02_hurst_regime_flip.py:S_TR02_hurst_regime_flip:HurstRegimeFlip"
  "str03_bocpd.py:S_TR03_bocpd:BOCPDDetector"
  "str04_zarattini_intraday_spy.py:S_TR04_zarattini_intraday_spy:ZarattiniIntradaySPY"
  "str06_baltussen_eod_reversal.py:S_TR06_baltussen_eod_reversal:BaltussenEODReversal"
)

MODE="shadow"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --shadow) MODE="shadow" ;;
    --paper)  MODE="paper"  ;;
    --live)   MODE="live"   ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

echo "===== FundJohn Top-10 Deploy ====="
echo "  src root     : $SRC_ROOT"
echo "  openclaw     : $OPENCLAW_ROOT"
echo "  target mode  : $MODE"
echo "  dry-run      : $DRY_RUN"
echo ""

# ── 1. SANITY ───────────────────────────────────────────────────────────────
echo "[1/8] sanity: verifying VPS paths and required data files"
for path in \
    "$OPENCLAW_ROOT" \
    "$OPENCLAW_ROOT/src/strategies" \
    "$OPENCLAW_ROOT/data/master"; do
  [[ -d "$path" ]] || { echo "  MISSING: $path" >&2; exit 2; }
done
# Warn (do not abort) if data files are not yet present — the data ingest
# scripts will create them.
for f in \
    "$OPENCLAW_ROOT/data/master/vol_indices.parquet" \
    "$OPENCLAW_ROOT/data/master/prices_30m.parquet" \
    "$OPENCLAW_ROOT/data/master/earnings_calendar.parquet" \
    "$OPENCLAW_ROOT/data/master/iv_history.parquet"; do
  [[ -f "$f" ]] || echo "  NOTE: $f not yet present — run ingest scripts after deploy"
done
echo ""

# ── 2. INSTALL STRATEGY FILES ───────────────────────────────────────────────
echo "[2/8] install: copy implementations/ → $IMPL_DST"
mkdir -p "$IMPL_DST"
for entry in "${STRATEGIES[@]}"; do
  filename="${entry%%:*}"
  if [[ ! -f "$SRC_ROOT/implementations/$filename" ]]; then
    echo "  MISSING: $filename" >&2
    exit 2
  fi
  if [[ "$DRY_RUN" -eq 0 ]]; then
    cp -v "$SRC_ROOT/implementations/$filename" "$IMPL_DST/$filename"
  else
    echo "  (dry) cp $filename"
  fi
done
echo ""

# ── 3. INSTALL ENGINE PATCH ─────────────────────────────────────────────────
echo "[3/8] install: engine_patches/aux_metrics.py → $PATCH_DST"
mkdir -p "$PATCH_DST"
if [[ "$DRY_RUN" -eq 0 ]]; then
  cp -v "$SRC_ROOT/engine_patches/aux_metrics.py" "$PATCH_DST/"
  [[ -f "$PATCH_DST/__init__.py" ]] || touch "$PATCH_DST/__init__.py"
else
  echo "  (dry) cp aux_metrics.py"
fi
echo ""

# ── 4. INSTALL INGEST SCRIPTS ──────────────────────────────────────────────
echo "[4/8] install: ingest/*.py → $INGEST_DST"
mkdir -p "$INGEST_DST"
if [[ "$DRY_RUN" -eq 0 ]]; then
  cp -v "$SRC_ROOT/ingest/"*.py "$INGEST_DST/"
else
  echo "  (dry) cp ingest/*.py"
fi
echo ""

# ── 5. REGISTER STRATEGIES ─────────────────────────────────────────────────
echo "[5/8] register: patch $REGISTRY"
python3 <<PYEOF
import json, os, sys
from pathlib import Path

reg_path = Path("$REGISTRY")
reg = {'strategies': []} if not reg_path.exists() else json.loads(reg_path.read_text())
existing_ids = {s.get('id') for s in reg.get('strategies', [])}
to_add = [
$(
  for entry in "${STRATEGIES[@]}"; do
    filename="${entry%%:*}";  rest="${entry#*:}"
    sid="${rest%%:*}";        cls="${rest#*:}"
    module="${filename%.py}"
    echo "    {'id': '$sid', 'class': '$cls', "
    echo "     'module': 'strategies.implementations.$module',"
    echo "     'file': '$filename',"
    echo "     'phase': '$MODE', 'version': '2.0.0',"
    echo "     'tags': ['top10_2026']},"
  done
)
]
for spec in to_add:
    if spec['id'] in existing_ids:
        # update in place
        for s in reg['strategies']:
            if s.get('id') == spec['id']:
                s.update(spec)
    else:
        reg['strategies'].append(spec)
if $DRY_RUN == 0:
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(reg, indent=2, sort_keys=True))
    print(f"  wrote {len(reg['strategies'])} strategies to {reg_path}")
else:
    print(f"  (dry) would write {len(reg['strategies'])} strategies")
PYEOF
echo ""

# ── 6. SET PHASE ───────────────────────────────────────────────────────────
echo "[6/8] set phase: $MODE in $PHASE_CFG"
python3 <<PYEOF
import json
from pathlib import Path
p = Path("$PHASE_CFG")
cfg = {'strategies': {}} if not p.exists() else json.loads(p.read_text())
cfg.setdefault('strategies', {})
for entry in """${STRATEGIES[@]}""".split():
    sid = entry.split(':')[1]
    cfg['strategies'][sid] = {
        'phase': "$MODE",
        'enabled': True,
        'notes': 'Top-10 cohort 2026-04; set phase via deploy_top10.sh',
    }
if $DRY_RUN == 0:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    print(f"  wrote phase config for {len(cfg['strategies'])} strategies")
else:
    print(f"  (dry) would set phase=$MODE for 10 strategies")
PYEOF
echo ""

# ── 7. SMOKE TEST ──────────────────────────────────────────────────────────
echo "[7/8] smoke test: import each strategy"
if [[ "$DRY_RUN" -eq 0 ]]; then
  cd "$OPENCLAW_ROOT"
  PYTHONPATH="$OPENCLAW_ROOT/src" python3 <<PYEOF
import importlib, sys, traceback

strategies = [
$(
  for entry in "${STRATEGIES[@]}"; do
    filename="${entry%%:*}";  rest="${entry#*:}"
    sid="${rest%%:*}";        cls="${rest#*:}"
    module="${filename%.py}"
    echo "    ('strategies.implementations.$module', '$cls'),"
  done
)
]
ok = 0; fail = 0
for mod, cls in strategies:
    try:
        m = importlib.import_module(mod)
        C = getattr(m, cls)
        inst = C()
        sigs = inst.generate_signals({}, {})
        assert isinstance(sigs, list), f'generate_signals must return list, got {type(sigs)}'
        print(f"  OK   {cls:30s}  signals={len(sigs)}")
        ok += 1
    except Exception as e:
        print(f"  FAIL {cls:30s}  {type(e).__name__}: {e}")
        traceback.print_exc()
        fail += 1
print(f"\nsmoke: {ok} ok, {fail} fail")
sys.exit(1 if fail else 0)
PYEOF
else
  echo "  (dry) skipping smoke test"
fi
echo ""

# ── 8. DEPLOY RECEIPT ──────────────────────────────────────────────────────
echo "[8/8] deploy receipt"
echo "  mode        : $MODE"
echo "  strategies  : ${#STRATEGIES[@]}"
echo "  registry    : $REGISTRY"
echo "  phase cfg   : $PHASE_CFG"
echo ""
echo "Next steps:"
echo "  1. Run ingest scripts on the cron (see top10_strategies_2026/TOP10_README.md):"
echo "       python3 $INGEST_DST/ingest_vol_indices.py"
echo "       python3 $INGEST_DST/ingest_prices_30m.py"
echo "       python3 $INGEST_DST/ingest_earnings_calendar.py"
echo "       python3 $INGEST_DST/ingest_iv_history.py"
echo "  2. Wire build_opts_map / build_market_data into engine.py per the"
echo "     ENGINE_PATCH_SNIPPET at the bottom of aux_metrics.py."
echo "  3. Watch Discord for first shadow-mode signals on next session."
echo ""
echo "===== done ====="
