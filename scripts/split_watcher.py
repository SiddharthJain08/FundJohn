"""SP-7 A3 — split-watcher (v2, 2026-07-03).

Split-adjusted history is stable EXCEPT at a split: a new split restates the
ticker's whole past series. Under the append-only invariant the remedy is the
sanctioned per-ticker supersede re-backfill (runbook v2 path:
OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 + --source-tag backfill_5y_vN +
--supersede-quarantine). This watcher DETECTS and QUEUES; the operator runs
the supersede (deliberate, audited).

v2 (post-UVIX incident 2026-07-01): the v1 source, corporate_actions.parquet,
had been dead since 2024 (25 rows, newest split ex_date 2024-06-10), so the
UVIX 1-for-20 reverse split sailed through undetected: prices.parquet now
carries a raw 20x discontinuity (3.09 -> 62.48) and the short's protective
stop was auto-cancelled on the effective date — both venue-independent
failure modes (they bite a live-money book identically). v2 therefore:
  * queries the Alpaca corporate-actions announcements API directly (the
    same CLI+creds the daily pipeline already uses); the stale parquet
    remains only as a fail-open fallback source;
  * looks AHEAD (today .. today+7): a split effective tomorrow alerts
    TONIGHT, while positions can still be flattened and before the price
    discontinuity lands;
  * cross-references broker positions and flags HELD tickers loudly —
    Alpaca cancels open orders (incl. protective stops) on the CA effective
    date, live and paper alike.

Run daily post-EOD (systemd user timer sp7-split-watcher, 21:15 UTC Mon-Fri;
unit loads .env so ALPACA_API_KEY/ALPACA_SECRET_KEY are present).

Webhook note
------------
#data-alerts in this repo is DB-backed (agent_registry.webhook_urls['data-alerts'],
read via run_collector_once.js). There is no direct DISCORD_DATA_ALERTS_WEBHOOK
env var in the production .env. This script therefore checks for an optional
DISCORD_DATA_ALERTS_WEBHOOK env var as a lightweight override (useful for
standalone / pre-merge invocations), and always writes to the durable queue
file regardless. Either way the primary artefact is the queue file, which
the operator must act on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path("/root/openclaw")
CORP_ACTIONS = ROOT / "data" / "master" / "corporate_actions.parquet"
PRICES = ROOT / "data" / "master" / "prices.parquet"
PENDING = ROOT / "data" / ".pending_split_rebackfills.txt"
ALPACA_BIN = os.environ.get("ALPACA_CLI_BIN", "/root/go/bin/alpaca")
LOOKAHEAD_DAYS = 7
# Optional env-var override for standalone use; repo canonical channel is
# DB-backed (agent_registry.webhook_urls['data-alerts']).
WEBHOOK_ENV = "DISCORD_DATA_ALERTS_WEBHOOK"


def _alpaca_cli(args: list[str], timeout: int = 30):
    """Run the alpaca CLI, JSON-parse stdout. Returns (ok, payload).

    The CLI signals errors as a JSON object with an 'error' key (exit code
    is unreliable) — check the key, never just count rows.
    """
    try:
        res = subprocess.run([ALPACA_BIN, *args], capture_output=True,
                             text=True, timeout=timeout)
        payload = json.loads(res.stdout or "null")
    except Exception as e:  # noqa: BLE001 — caller falls open to legacy source
        return False, f"cli failed: {e}"
    if isinstance(payload, dict) and payload.get("error"):
        return False, payload["error"]
    return True, payload


def _fmt_ratio(old_rate, new_rate) -> str:
    try:
        old_f, new_f = float(old_rate), float(new_rate)
    except (TypeError, ValueError):
        return "?"
    if old_f > new_f:
        return f"1-for-{old_f / max(new_f, 1e-9):g} (REVERSE)"
    return f"{new_f / max(old_f, 1e-9):g}-for-1"


def fetch_announced_splits(since: str, until: str) -> list[dict] | None:
    """Alpaca corporate-action announcements in [since, until]. None = source
    unavailable (caller falls open to the legacy parquet path)."""
    ok, payload = _alpaca_cli(["corporate-action", "list", "--ca-types", "Split",
                               "--since", since, "--until", until])
    if not ok:
        print(f"[split-watcher] announcements API unavailable ({payload}); "
              f"falling back to corporate_actions.parquet")
        return None
    out = []
    for a in payload or []:
        sym = a.get("target_symbol") or a.get("initiating_symbol") or ""
        ex = str(a.get("ex_date") or a.get("effective_date") or "")
        if not sym or not ex:
            continue
        out.append({
            "symbol": sym,
            "ex_date": ex,
            "old_rate": a.get("old_rate"),
            "new_rate": a.get("new_rate"),
            "ratio": _fmt_ratio(a.get("old_rate"), a.get("new_rate")),
            "sub_type": a.get("ca_sub_type") or "split",
        })
    return out


def held_tickers() -> set[str]:
    """Current broker position symbols (empty set on any failure)."""
    ok, payload = _alpaca_cli(["position", "list"])
    if not ok or not isinstance(payload, list):
        return set()
    return {p.get("symbol") for p in payload if p.get("symbol")}


def covered_tickers() -> set[str]:
    return set(pd.read_parquet(PRICES, columns=["ticker"]).ticker.unique())


def find_new_splits(today: str) -> list[dict]:
    """Legacy v1 source: corporate_actions.parquet, ex_date == today only.
    Retained solely as the fail-open fallback when the API is unreachable.
    (Known-stale: newest split row is from 2024 — see module docstring.)"""
    ca = pd.read_parquet(CORP_ACTIONS,
                         columns=["symbol", "action_type", "ex_date", "ratio"])
    splits = ca[ca.action_type.astype(str).str.contains("split", case=False)
                & (ca.ex_date.astype(str) == today)]
    if splits.empty:
        return []
    covered = covered_tickers()
    return [r for r in splits.to_dict("records") if r["symbol"] in covered]


def notify(msg: str) -> None:
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        print(f"[split-watcher] (no webhook env var set) {msg}")
        return
    body = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 # Explicit UA avoids Cloudflare 1010 (urllib default UA banned).
                 "User-Agent": "openclaw-split-watcher/1.0"},
    )
    urllib.request.urlopen(req, timeout=15)


def _already_queued(line_key: str) -> bool:
    if not PENDING.exists():
        return False
    return any(line_key in line for line in PENDING.read_text().splitlines())


def main() -> int:
    today_d = date.today()
    today = today_d.isoformat()
    until = (today_d + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    # Look slightly back too: a weekend/holiday/outage gap must not turn a
    # missed ex-date into a permanently silent discontinuity.
    since = (today_d - timedelta(days=3)).isoformat()

    announced = fetch_announced_splits(since, until)
    if announced is None:
        hits = find_new_splits(today)          # legacy fallback path
        upcoming: list[dict] = []
    else:
        covered = covered_tickers()
        in_scope = [a for a in announced if a["symbol"] in covered]
        hits = [a for a in in_scope if a["ex_date"] <= today]
        upcoming = [a for a in in_scope if a["ex_date"] > today]

    if not hits and not upcoming:
        print(f"[split-watcher] {today}: no splits on covered tickers "
              f"(window {since}..{until})")
        return 0

    lines: list[str] = []
    if hits:
        PENDING.parent.mkdir(exist_ok=True)
        queued = []
        with PENDING.open("a") as f:
            for h in hits:
                key = f"{h['symbol']} ex={h['ex_date']}"
                if _already_queued(key):
                    continue
                f.write(f"{today} {h['symbol']} ex={h['ex_date']} "
                        f"ratio={h.get('ratio')}\n")
                queued.append(h)
        if queued:
            syms = ", ".join(f"{h['symbol']} ({h.get('ratio', '?')})" for h in queued)
            lines.append(
                f"\U0001fa93 Split EFFECTIVE on covered ticker(s): **{syms}** — "
                f"history is now stale-adjusted. Queue written to {PENDING}. "
                f"Run the supersede re-backfill per docs/runbooks/sp2-backfill-runbook.md (v2 path).")

    if upcoming:
        held = held_tickers()
        for a in upcoming:
            tag = ""
            if a["symbol"] in held:
                tag = (" ⚠️ **HELD** — open orders (incl. protective stops) are "
                       "auto-cancelled on the effective date; flatten or re-protect "
                       "before the prior close (shorts in leveraged ETPs especially).")
            lines.append(f"\U0001f4c5 Upcoming split: **{a['symbol']}** "
                         f"{a.get('ratio', '?')} effective {a['ex_date']}.{tag}")

    if lines:
        notify("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
