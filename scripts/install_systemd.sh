#!/usr/bin/env bash
# install_systemd.sh — install every OpenClaw systemd unit from docs/systemd/.
#
# docs/systemd/ is the canonical, byte-exact snapshot of the deployed units
# (see docs/systemd/README.md). This script replaces the old README cp
# one-liners, which failed three ways: the openclaw-* glob skipped
# finbert-sentiment/fundjohn-dashboard/mastermind-chat, the johnbot OOM
# drop-in landed in system scope while the live johnbot runs in ROOT USER
# scope, and the weekend-swap timer overrides need a name transformation
# (X.timer.override.conf -> X.timer.d/override.conf) no cp can perform.
#
# Usage: sudo bash scripts/install_systemd.sh
# Idempotent — safe to re-run after changing any unit under docs/systemd/.
#
# NOT installed by design:
#   - johnbot.service in SYSTEM scope. The live bot is the USER-scope unit
#     (~/.config/systemd/user/johnbot.service). A system-scope copy causes a
#     split-brain double bot — never enable one.
# After installing, ENABLE only the units you want live — see the
# "Enablement set" section in docs/bootstrap.md. Timers with Persistent=true
# fire a catch-up run the moment they are enabled; touch their stamp files
# first when that would be harmful (see docs/systemd/README.md).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/docs/systemd"
SYS=/etc/systemd/system
USER_UNIT_DIR="$HOME/.config/systemd/user"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "== system-scope services + timers =="
for f in "$SRC"/*.service "$SRC"/*.timer; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  # johnbot.service (system scope) is deliberately not shipped; nothing to skip
  install -m 644 "$f" "$SYS/$base"
done

echo "== system-scope drop-in directories =="
for d in "$SRC"/*.service.d; do
  [ -d "$d" ] || continue
  base="$(basename "$d")"
  mkdir -p "$SYS/$base"
  install -m 644 "$d"/*.conf "$SYS/$base/"
done

echo "== weekend-swap timer overrides (X.timer.override.conf -> X.timer.d/override.conf) =="
if [ -d "$SRC/weekend-swap" ]; then
  for f in "$SRC/weekend-swap"/*.timer.override.conf; do
    [ -e "$f" ] || continue
    timer="$(basename "$f" .override.conf)"          # openclaw-foo.timer
    mkdir -p "$SYS/$timer.d"
    install -m 644 "$f" "$SYS/$timer.d/override.conf"
  done
fi

echo "== user-scope units (root user scope — the live johnbot lives here) =="
mkdir -p "$USER_UNIT_DIR"
for f in "$SRC/user"/*.service "$SRC/user"/*.timer; do
  [ -e "$f" ] || continue
  install -m 644 "$f" "$USER_UNIT_DIR/$(basename "$f")"
done
for d in "$SRC/user"/*.service.d; do
  [ -d "$d" ] || continue
  base="$(basename "$d")"
  mkdir -p "$USER_UNIT_DIR/$base"
  install -m 644 "$d"/*.conf "$USER_UNIT_DIR/$base/"
done

echo "== daemon-reload (both scopes) =="
systemctl daemon-reload
XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user daemon-reload || \
  echo "note: user-scope reload failed — run 'systemctl --user daemon-reload' in a login session"

echo "done. Units installed; nothing was enabled or started."
echo "Next: follow the enablement set in docs/bootstrap.md."
