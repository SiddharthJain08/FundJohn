#!/usr/bin/env bash
# scripts/epoch_universe_parity.sh — operator tooling for the backtest UNIVERSE
# PARITY epoch (Phase 3, 2026-10-06; runbook docs/superpowers/plans/2026-10-06-universe-parity-epoch.md).
#
# Goal: every fleet backtest bounds its universe by the strategy's OWN manifest
# universe_filter_ref (OPENCLAW_BT_UNIVERSE_FILTER_REF=1 on the FLEET units only —
# the code default stays OFF and the live engine is untouched), then the fleet is
# re-gated once. Weights/activation are rebuilt only after the operator has read
# the before/after report.
#
# Modelled on the 2026-09-10 target-geometry epoch. Subcommands:
#   checkpoint   copy data/.refresh_backtests.done{,.failed} -> .<tag>   (runs; refuses to overwrite)
#   rotate       move the two live ledgers aside so the driver sees done=0 (needs --run;
#                requires the checkpoint copies to exist and be identical)
#   artifact     print / (--run) execute the transient-unit build of the membership artifact
#                universe_tier_membership_shrink-<date>.parquet (refuses to overwrite)
#   install      print / (--run) write + install the overnight-resume drop-in
#                universe-parity.conf and the weekend window unit
#                fleet-universe-parity-epoch-<date>.service (+ optional .timer); daemon-reload.
#                Never starts or enables anything — it prints the start command.
#   status       run scripts/universe_parity_gate.py (read-only; runs unless --dry-run)
#   report       run scripts/universe_parity_report.py (read-only; runs unless --dry-run)
#   uninstall    print / (--run) remove the drop-in + epoch unit(s); daemon-reload
#                (--restore-ledgers also copies the checkpoint ledgers back, never overwriting
#                a live file that is not already rotated)
#
# Options (anywhere):
#   --run               actually mutate (install/artifact/rotate/uninstall); default prints the plan
#   --dry-run           with checkpoint/status/report: print instead of executing
#   --date YYYYMMDD     epoch date (default today UTC); tag = pre-universe-parity-<date>
#   --root DIR          repo root (default /root/openclaw)       [testability]
#   --etc DIR           systemd unit dir (default /etc/systemd/system) [testability]
#   --deadline UTC      install: YYYY-MM-DDTHH:MM, no NEW strategy spawned after it (REQUIRED for install)
#   --on-calendar SPEC  install: also write a .timer firing the epoch unit (systemd calendar spec)
#   --runtime-max SEC   install: RuntimeMaxSec of the weekend unit (default 99000 = 27.5 h)
#   --end YYYY-MM-DD    artifact: window end (default = --date)
#   --force-order       rotate: skip the 'drop-in must be installed first' guard (tests/emergencies)
#   --restore-ledgers   uninstall: also restore the checkpoint ledgers
#
# Never sources or cats .env: units reference it via EnvironmentFile=; the one read is a
# `grep -q` for the flag NAME (existence only) because EnvironmentFile wins over Environment=.
set -u
ROOT=/root/openclaw; ETC=/etc/systemd/system; RUN=0; DRY=0; DATE=""; DEADLINE=""
ONCAL=""; RTMAX=99000; END=""; RESTORE=0; SUB=""; FORCE_ORDER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --run) RUN=1;; --dry-run) DRY=1;; --restore-ledgers) RESTORE=1;; --force-order) FORCE_ORDER=1;;
    --date) DATE="$2"; shift;; --root) ROOT="$2"; shift;; --etc) ETC="$2"; shift;;
    --deadline) DEADLINE="$2"; shift;; --on-calendar) ONCAL="$2"; shift;;
    --runtime-max) RTMAX="$2"; shift;; --end) END="$2"; shift;;
    -h|--help) sed -n 2,40p "$0"; exit 0;;
    -*) echo "unknown option $1" >&2; exit 2;;
    *) [ -z "$SUB" ] && SUB="$1" || { echo "unexpected arg $1" >&2; exit 2; };;
  esac; shift
done
[ -n "$SUB" ] || { echo "usage: $0 {checkpoint|rotate|artifact|install|status|report|uninstall} [--run] ..." >&2; exit 2; }
DATE_GIVEN=1; [ -n "$DATE" ] || { DATE_GIVEN=0; DATE=$(date -u +%Y%m%d); }
case "$DATE" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) echo "bad --date $DATE (want YYYYMMDD)" >&2; exit 2;; esac
[ -n "$END" ] || END="${DATE:0:4}-${DATE:4:2}-${DATE:6:2}"
if [ "$SUB" = uninstall ] && [ "$DATE_GIVEN" = 0 ]; then
  # never default to today: find what the install/checkpoint actually used
  found=$( { ls "$ETC"/fleet-universe-parity-epoch-*.service "$ETC"/fleet-universe-parity-epoch-*.timer 2>/dev/null \
               | sed -E 's/.*epoch-([0-9]{8})\.(service|timer)$/\1/'
             ls "$ROOT"/data/.refresh_backtests.done.pre-universe-parity-* 2>/dev/null \
               | sed -E -n 's/.*pre-universe-parity-([0-9]{8})$/\1/p'; } | sort -u)
  n=$(printf '%s' "$found" | grep -c .)
  if [ "$n" -gt 1 ]; then echo "REFUSING: several epochs found ($(echo $found)); pass --date YYYYMMDD" >&2; exit 2
  elif [ "$n" = 1 ]; then DATE=$found; echo "[uninstall] epoch date resolved to $DATE"; fi
fi
PY=${PYTHON:-python3}
TAG="pre-universe-parity-$DATE"
DATA="$ROOT/data"; DONE="$DATA/.refresh_backtests.done"; FAILED="$DATA/.refresh_backtests.done.failed"
ARTIFACT="$DATA/universe_tier_membership_shrink-$DATE.parquet"
DROPIN_DIR_NAME="openclaw-fleet-overnight-resume.service.d"
DROPIN_SNAP="$ROOT/docs/systemd/$DROPIN_DIR_NAME/universe-parity.conf"
DROPIN_ETC="$ETC/$DROPIN_DIR_NAME/universe-parity.conf"
EPOCH_UNIT="fleet-universe-parity-epoch-$DATE"
EPOCH_UNIT_ETC="$ETC/$EPOCH_UNIT.service"; EPOCH_TIMER_ETC="$ETC/$EPOCH_UNIT.timer"
FLAG=OPENCLAW_BT_UNIVERSE_FILTER_REF

say()  { echo "$*"; }
plan() { echo "[plan] $*"; }
need_run() { # print the plan header for mutating subcommands
  [ "$RUN" = 1 ] && echo "[run] $SUB" || echo "[dry-run] $SUB — nothing will change; add --run to execute"; }

dropin_content() {
  printf '%s\n' '[Service]' \
    '# Backtest universe parity epoch ('"$DATE"'). The fleet re-backtest bounds each strategy'"'"'s universe by its' \
    '# manifest universe_filter_ref so every canonical row carries config_json.universe_bound_source.' \
    '# EnvironmentFile (.env) wins over Environment=; .env must NOT define '"$FLAG"'. Remove with:' \
    '#   scripts/epoch_universe_parity.sh uninstall --date '"$DATE"' --run' \
    'Environment="'"$FLAG"'=1"'
}

epoch_unit_content() {
  printf '%s\n' '[Unit]' \
    "Description=Fleet re-backtest weekend window under universe_filter_ref parity (epoch $DATE)" \
    '' '[Service]' 'Nice=19' 'OOMPolicy=continue' "RuntimeMaxSec=$RTMAX" \
    'EnvironmentFile=' "EnvironmentFile=$ROOT/.env" "WorkingDirectory=$ROOT" \
    "Environment=\"NUMEXPR_MAX_THREADS=1\" \"NUMEXPR_NUM_THREADS=1\" \"$FLAG=1\" \"PYTHONUNBUFFERED=1\"" \
    'ExecStart=' "ExecStart=\"/bin/bash\" \"$ROOT/scripts/fleet_weekend_window.sh\" \"--deadline\" \"$DEADLINE\""
}
epoch_timer_content() {
  printf '%s\n' '[Unit]' "Description=Start $EPOCH_UNIT" '' '[Timer]' "OnCalendar=$ONCAL" \
    'Persistent=false' "Unit=$EPOCH_UNIT.service" '' '[Install]' 'WantedBy=timers.target'
}

env_file_flag_warning() {
  if [ -f "$ROOT/.env" ] && grep -q "^$FLAG=" "$ROOT/.env" 2>/dev/null; then
    echo "WARNING: $ROOT/.env defines $FLAG — EnvironmentFile wins over Environment=, so the drop-in/unit value would be ignored. Resolve before the epoch." >&2
  fi
}

case "$SUB" in
checkpoint)
  echo "[checkpoint] tag=$TAG"
  for f in "$DONE" "$FAILED"; do
    dst="$f.$TAG"
    if [ -e "$dst" ]; then echo "REFUSING to overwrite existing $dst" >&2; exit 1; fi
    if [ ! -e "$f" ]; then echo "  (missing $f — nothing to copy)"; continue; fi
    if [ "$DRY" = 1 ]; then plan "cp $f $dst"; else cp "$f" "$dst" && say "  copied $f -> $dst ($(wc -l < "$dst") lines)"; fi
  done ;;
rotate)
  need_run
  for f in "$DONE" "$FAILED"; do
    [ -e "$f" ] || { say "  (missing $f — skipped)"; continue; }
    ck="$f.$TAG"; dst="$f.rotated-$TAG"
    if [ "$FORCE_ORDER" != 1 ] && [ ! -e "$DROPIN_ETC" ]; then
      echo "REFUSING: drop-in $DROPIN_ETC not installed. Order is checkpoint -> artifact -> install --run -> rotate --run; rotating first lets the 21:30Z nightly re-run the fleet WITHOUT the flag. (--force-order overrides)" >&2; exit 1
    fi
    [ -e "$ck" ] || { echo "REFUSING: checkpoint $ck missing — run 'checkpoint' first" >&2; exit 1; }
    cmp -s "$f" "$ck" || { echo "REFUSING: $f differs from its checkpoint $ck (driver ran since?)" >&2; exit 1; }
    [ -e "$dst" ] && { echo "REFUSING to overwrite $dst" >&2; exit 1; }
    plan "mv $f $dst   (the driver then sees done=0; the checkpoint copy is untouched)"
    [ "$RUN" = 1 ] && mv "$f" "$dst"
  done
  SINCE_F="$DONE.$TAG.since"; NOWISO=$(date -u +%Y-%m-%dT%H:%M)
  if [ "$RUN" = 1 ]; then
    [ -e "$SINCE_F" ] && { echo "REFUSING to overwrite $SINCE_F" >&2; exit 1; }
    echo "$NOWISO" > "$SINCE_F"
    say "epoch start stamped: $NOWISO UTC -> $SINCE_F (gate/report read it by default; or pass --since $NOWISO)"
    say "the next 21:30Z nightly now starts the flagged epoch by itself (drop-in + done=0)."
  else
    plan "stamp the rotate time (ISO UTC) into $SINCE_F"
  fi ;;
artifact)
  need_run
  if [ -e "$ARTIFACT" ]; then echo "REFUSING to overwrite existing $ARTIFACT (pick another --date)" >&2; exit 1; fi
  CMD=(systemd-run --wait --pipe --collect "--unit=epoch-tier-membership-$DATE"
       "--property=EnvironmentFile=$ROOT/.env" --property=Nice=19 --property=MemoryMax=3500M
       "--property=WorkingDirectory=$ROOT" --setenv=PYTHONPATH=src
       /usr/bin/python3 scripts/build_tier_membership.py --run-id "shrink-$DATE"
       --start 2016-03-01 --end "$END" --out-dir data)
  plan "${CMD[*]}"
  plan "output: $ARTIFACT (+ .json sidecar); the newest universe_tier_membership_shrink-*.parquet is what _bounded_resolver picks"
  [ "$RUN" = 1 ] && "${CMD[@]}" ;;
install)
  need_run
  [ -n "$DEADLINE" ] || { echo "--deadline YYYY-MM-DDTHH:MM (UTC) is required for install" >&2; exit 2; }
  case "$DEADLINE" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]) ;; *) echo "bad --deadline $DEADLINE" >&2; exit 2;; esac
  env_file_flag_warning
  say "--- $DROPIN_SNAP  ->  $DROPIN_ETC"; dropin_content
  say "--- $EPOCH_UNIT_ETC"; epoch_unit_content
  if [ -n "$ONCAL" ]; then say "--- $EPOCH_TIMER_ETC"; epoch_timer_content; fi
  if [ "$RUN" = 1 ]; then
    mkdir -p "$(dirname "$DROPIN_SNAP")" "$(dirname "$DROPIN_ETC")" "$ETC"
    for p in "$DROPIN_SNAP" "$DROPIN_ETC" "$EPOCH_UNIT_ETC"; do
      [ -e "$p" ] && { echo "REFUSING to overwrite existing $p" >&2; exit 1; }
    done
    dropin_content > "$DROPIN_SNAP"; cp "$DROPIN_SNAP" "$DROPIN_ETC"
    epoch_unit_content > "$EPOCH_UNIT_ETC"
    [ -n "$ONCAL" ] && epoch_timer_content > "$EPOCH_TIMER_ETC"
    if [ "${NO_SYSTEMCTL:-0}" != 1 ]; then systemctl daemon-reload; fi
    say "installed. NOT started. Start the window yourself:"
  else
    plan "write the files above, daemon-reload"
    say "after --run, start the window yourself:"
  fi
  if [ -n "$ONCAL" ]; then say "  systemctl start $EPOCH_UNIT.timer"; else say "  systemctl start --no-block $EPOCH_UNIT.service"; fi ;;
status)
  CMD=("$PY" "$ROOT/scripts/universe_parity_gate.py" --env-file "$ROOT/.env"
       --manifest "$ROOT/src/strategies/manifest.json" --data-dir "$DATA")
  if [ "$DRY" = 1 ]; then plan "${CMD[*]}"; else "${CMD[@]}"; fi ;;
report)
  OUT_BASE="$ROOT/docs/superpowers/plans/universe-parity-report-$DATE"
  CMD=("$PY" "$ROOT/scripts/universe_parity_report.py" --env-file "$ROOT/.env"
       --manifest "$ROOT/src/strategies/manifest.json" --data-dir "$DATA" --out "$OUT_BASE")
  if [ "$DRY" = 1 ]; then plan "${CMD[*]}"; else "${CMD[@]}"; fi ;;
uninstall)
  need_run
  for p in "$DROPIN_ETC" "$DROPIN_SNAP" "$EPOCH_UNIT_ETC" "$EPOCH_TIMER_ETC"; do
    if [ -e "$p" ]; then plan "rm $p"; [ "$RUN" = 1 ] && rm -f "$p"; else say "  (absent: $p)"; fi
  done
  if [ "$RUN" = 1 ] && [ "${NO_SYSTEMCTL:-0}" != 1 ]; then systemctl daemon-reload; fi
  [ "$RUN" = 1 ] && say "stop any running window yourself: systemctl stop $EPOCH_UNIT.service"
  if [ "$RESTORE" = 1 ]; then
    for f in "$DONE" "$FAILED"; do
      ck="$f.$TAG"
      [ -e "$ck" ] || { say "  (no checkpoint $ck)"; continue; }
      if [ -e "$f" ]; then
        plan "SKIP restore of $f — a live ledger exists (merge by hand: cat $ck >> after reviewing)"
      else
        plan "cp $ck $f"; [ "$RUN" = 1 ] && cp "$ck" "$f"
      fi
    done
  fi ;;
*) echo "unknown subcommand $SUB" >&2; exit 2;;
esac
exit 0
