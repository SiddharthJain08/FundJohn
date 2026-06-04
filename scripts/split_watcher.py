"""SP-7 A3 — split-watcher.

Split-adjusted history is stable EXCEPT at a split: a new split restates the
ticker's whole past series. Under the append-only invariant the remedy is the
sanctioned per-ticker supersede re-backfill (runbook v2 path:
OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 + --source-tag backfill_5y_vN +
--supersede-quarantine). This watcher DETECTS and QUEUES; the operator runs
the supersede (deliberate, audited).

Run daily post-EOD (systemd timer, 21:15 UTC weekdays).

Webhook note
------------
#data-alerts in this repo is DB-backed (agent_registry.webhook_urls['data-alerts'],
read via run_collector_once.js). There is no direct DISCORD_DATA_ALERTS_WEBHOOK
env var in the production .env. This script therefore checks for an optional
DISCORD_DATA_ALERTS_WEBHOOK env var as a lightweight override (useful for
standalone / pre-merge invocations), and always writes to the durable queue
file regardless. The DB-backed post will be wired at merge via the standard
orchestrator path. Either way the primary artefact is the queue file, which
the operator must act on.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path("/root/openclaw")
CORP_ACTIONS = ROOT / "data" / "master" / "corporate_actions.parquet"
PRICES = ROOT / "data" / "master" / "prices.parquet"
PENDING = ROOT / "data" / ".pending_split_rebackfills.txt"
# Optional env-var override for standalone use; repo canonical channel is
# DB-backed (agent_registry.webhook_urls['data-alerts']).
WEBHOOK_ENV = "DISCORD_DATA_ALERTS_WEBHOOK"


def find_new_splits(today: str) -> list[dict]:
    ca = pd.read_parquet(CORP_ACTIONS,
                         columns=["symbol", "action_type", "ex_date", "ratio"])
    splits = ca[ca.action_type.astype(str).str.contains("split", case=False)
                & (ca.ex_date.astype(str) == today)]
    if splits.empty:
        return []
    covered = set(pd.read_parquet(PRICES, columns=["ticker"]).ticker.unique())
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


def main() -> int:
    today = date.today().isoformat()
    hits = find_new_splits(today)
    if not hits:
        print(f"[split-watcher] {today}: no splits on covered tickers")
        return 0
    PENDING.parent.mkdir(exist_ok=True)
    with PENDING.open("a") as f:
        for h in hits:
            f.write(f"{today} {h['symbol']} ratio={h.get('ratio')}\n")
    syms = ", ".join(h["symbol"] for h in hits)
    notify(f"\U0001fa93 Split detected on covered ticker(s): **{syms}** — history is "
           f"now stale-adjusted. Queue written to {PENDING}. Run the supersede "
           f"re-backfill per docs/sp2-backfill-runbook.md (v2 path).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
