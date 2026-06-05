#!/usr/bin/env bash
# SP-6 B-flow — WEEKLY Phase-1b re-run (Saturday).
#
# Re-runs the order-flow predictability kill-test (run_phase1b) over the
# accrued minute-bar cache and posts a COMPACT summary (verdict + PRIMARY grid,
# plus an OOS section if/when run_phase1b grows one) to Discord #data-alerts.
#
# CACHE-ONLY: run_phase1b never fetches — it reads the shared cache that the
# nightly bflow-minbar-accrual job grows. We do NOT pass --cache-dir so the
# module default (the shared /root/openclaw/data/cache/min_bars) is used —
# the SAME dir the accrual writes.
#
# Exit code is captured WITHOUT a pipe-to-tail (a pipe would mask python's exit
# behind tail's — the 2026-06 lesson): stdout is redirected to a temp file, $?
# is read directly, then the file is parsed for the summary.
#
# --oos-start: the headline OOS window starts 2026-06-08. run_phase1b does NOT
# (as of this commit) accept an --oos-start flag (verified against the module +
# git log) — so it is OMITTED here. When the sibling build adds it, append
# `--oos-start 2026-06-08` to the RUN_CMD below.
#
# Discord post: python3 + requests + explicit browser-like User-Agent (the
# default python-urllib UA gets Cloudflare-1010 403'd — ops lesson 2026-06-01).
set -uo pipefail

WORKTREE="/root/.config/superpowers/worktrees/sp6-bflow-phase1-oracle"
OUT="$(mktemp /tmp/bflow_weekly_rerun.XXXXXX.log)"
trap 'rm -f "$OUT"' EXIT

cd "$WORKTREE" || { echo "[bflow-weekly] cannot cd $WORKTREE"; exit 1; }

# Run the kill-test. Capture exit code directly (no pipe-to-tail).
PYTHONPATH=src nice -n 19 python3 -u -m research.bflow.run_phase1b >"$OUT" 2>&1
RC=$?

echo "[bflow-weekly] run_phase1b exit=$RC"
cat "$OUT"

# Build + post the compact summary (or a FAILURE line) via requests.
OUT_PATH="$OUT" RC="$RC" python3 - <<'PY'
import json, os, re, sys

out_path = os.environ["OUT_PATH"]
rc = int(os.environ["RC"])
text = open(out_path, errors="ignore").read()


def webhook_url():
    """data-alerts webhook from agent_registry (same source the SP-6 phase-C
    verify + fold_report use)."""
    import psycopg2
    uri = None
    for line in open("/root/openclaw/.env"):
        if line.startswith("POSTGRES_URI="):
            uri = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not uri:
        return None
    try:
        c = psycopg2.connect(uri).cursor()
        c.execute("SELECT webhook_urls->>'data-alerts' "
                  "FROM agent_registry WHERE id='botjohn'")
        row = c.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"[bflow-weekly] webhook lookup failed: {e}")
        return None


def post(text):
    url = webhook_url()
    if not url:
        print("[bflow-weekly] no data-alerts webhook; not posting.")
        return
    import requests
    try:
        r = requests.post(
            url,
            data=json.dumps({"content": text[:1900]}),
            headers={
                "Content-Type": "application/json",
                # explicit browser-like UA — default urllib UA = CF-1010 403.
                "User-Agent": "OpenClaw-SP6BflowWeekly/1.0 (+botjohn)",
            },
            timeout=15,
        )
        if r.status_code >= 300:
            print(f"[bflow-weekly] webhook post HTTP {r.status_code}: "
                  f"{r.text[:200]}")
    except Exception as e:
        print(f"[bflow-weekly] webhook post failed: {e}")


if rc != 0:
    post(f"🔴 **SP-6 B-flow weekly re-run FAILED** (run_phase1b exit={rc})\n"
         "```\n" + text[-1200:] + "\n```")
    sys.exit(0)

# verdict line (the durable [bflow-p1b] VERDICT: <X> stdout marker).
m = re.search(r"^\[bflow-p1b\] VERDICT:\s*(\S+)", text, re.M)
verdict = m.group(1) if m else "?"

# PRIMARY grid block: from the '## PRIMARY grid' header to the next blank line.
grid_lines = []
in_grid = False
for line in text.splitlines():
    if line.startswith("## PRIMARY grid"):
        in_grid = True
        grid_lines.append(line.replace("## ", "").strip())
        continue
    if in_grid:
        if line.strip() == "":
            break
        grid_lines.append(line)
grid_block = "\n".join(grid_lines) if grid_lines else "(PRIMARY grid not found)"

# OOS section if run_phase1b ever grows one (matched defensively on a header
# containing 'OOS'; absent today — noted in the script header).
oos_lines = []
in_oos = False
for line in text.splitlines():
    if line.startswith("##") and "OOS" in line.upper():
        in_oos = True
        oos_lines.append(line.replace("## ", "").strip())
        continue
    if in_oos:
        if line.startswith("## "):
            break
        oos_lines.append(line)
oos_block = ("\n" + "\n".join(oos_lines).rstrip()) if oos_lines else ""

emoji = {"GO": "🟢", "KILL": "🔴", "WEAK": "🟡"}.get(verdict, "⚪")
body = (f"{emoji} **SP-6 B-flow weekly re-run** — VERDICT: {verdict}\n"
        f"{grid_block}{oos_block}\n"
        "_Test A (clustered across-session IC) is the only gate; "
        "in-sample, hypothesis-generating — NOT a working config._")
print(body)
post(body)
PY

exit "$RC"
