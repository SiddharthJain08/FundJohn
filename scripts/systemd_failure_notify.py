#!/usr/bin/env python3
"""Post a Discord notification when a systemd unit fails.

Wired in via `OnFailure=openclaw-failure-notify@%n.service` on each unit we
care about. The %n token expands to the failed unit name (e.g.
`openclaw-botjohn-maintenance.service`), which systemd passes to the
template service as %i and which the .service unit forwards as argv[1].

This script is intentionally standalone (stdlib + psycopg2-binary only)
so it cannot be killed by whatever bug killed the unit it's reporting on.
That was the 2026-05-13..18 silence: a SyntaxError in run_maintenance.js
killed the script *before* its own Discord-fallback could fire.

Usage:
    systemd_failure_notify.py <unit-name>
"""
import json
import os
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def lookup_webhook() -> str | None:
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        print("[failure-notify] POSTGRES_URI unset; cannot look up webhook", file=sys.stderr)
        return None
    try:
        import psycopg2
    except ImportError:
        print("[failure-notify] psycopg2 not available", file=sys.stderr)
        return None
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor()
        cur.execute("SELECT webhook_urls FROM agent_registry WHERE id=%s", ("botjohn",))
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        urls = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return urls.get("botjohn-log") or urls.get("general") or next(iter(urls.values()), None)
    except Exception as e:
        print(f"[failure-notify] webhook lookup error: {e}", file=sys.stderr)
        return None


def get_unit_state(unit: str) -> dict:
    """Pull Result, ActiveEnterTimestamp, ExecMainStatus, etc. for context."""
    try:
        out = subprocess.run(
            ["systemctl", "show", unit,
             "-p", "Result", "-p", "ExecMainStatus", "-p", "ActiveEnterTimestamp",
             "-p", "InactiveEnterTimestamp", "-p", "Description"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    except Exception:
        return {}


def get_tail(unit: str, lines: int = 25) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return out.strip()
    except Exception as e:
        return f"(journalctl failed: {e})"


def post(url: str, content: str) -> bool:
    body = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            # Discord proxies through Cloudflare which 403s python-urllib/*
            # with error code 1010. Identify as the notifier so the request
            # passes the bot/UA filter.
            "User-Agent": "OpenClaw-SystemdFailureNotifier/1.0 (+botjohn)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[failure-notify] POST failed: {e}", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: systemd_failure_notify.py <unit-name>", file=sys.stderr)
        return 2
    unit = argv[1]
    state = get_unit_state(unit)
    tail = get_tail(unit, lines=25)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    host = socket.gethostname()

    desc   = state.get("Description", unit)
    result = state.get("Result", "unknown")
    exit_  = state.get("ExecMainStatus", "?")

    # Discord 1900-char budget; reserve ~400 for header + footer, rest for tail.
    header = (
        f"🚨 **systemd unit failed** — `{unit}`\n"
        f"_{desc}_\n"
        f"`Result={result} ExecMainStatus={exit_}` on `{host}` at {now}\n"
        f"```\n"
    )
    footer = "\n```"
    budget = 1900 - len(header) - len(footer)
    tail_clipped = tail[-budget:] if len(tail) > budget else tail
    content = header + tail_clipped + footer

    webhook = lookup_webhook()
    if not webhook:
        # Log loudly so journalctl on the notifier itself shows the silence.
        print(f"[failure-notify] FATAL: no webhook URL; would have posted:\n{content}",
              file=sys.stderr)
        return 1

    ok = post(webhook, content)
    if not ok:
        return 1
    print(f"[failure-notify] posted {len(content)} chars for {unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
