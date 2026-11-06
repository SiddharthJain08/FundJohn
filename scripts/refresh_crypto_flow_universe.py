#!/usr/bin/env python3
"""Refresh src/ingestion/crypto_flow/crypto_flow_universe.json from Alpaca's crypto asset list.

Dry-run by default; ``--apply`` writes the file atomically (tmp + os.replace).
Rows are never removed (append-only invariant): new pairs are appended with
venue fields null (fill by hand), pairs missing from Alpaca flip to active=false.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.crypto_flow import universe as u  # noqa: E402

ALPACA = "/root/go/bin/alpaca"


def fetch_alpaca_symbols() -> list[str]:
    out = subprocess.run(
        [ALPACA, "asset", "list", "--asset-class", "crypto", "--status", "active", "-q",
         "--jq", '[.[]|select(.symbol|endswith("/USD")).symbol]'],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the JSON (default: dry-run)")
    args = ap.parse_args(argv)

    path = u.UNIVERSE_PATH
    existing = u.load_universe(path, active_only=False)
    merged, added, deact = u.refresh_from_alpaca(existing, fetch_alpaca_symbols())
    print(f"[crypto-universe] added={added} deactivated={deact} total={len(merged)}")
    if not args.apply:
        return 0
    meta = json.loads(path.read_text()).get("_meta", {})
    meta["updated"] = date.today().isoformat()
    payload = {"_meta": meta, "assets": [dataclasses.asdict(a) for a in merged]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n")
    os.replace(tmp, path)
    print(f"[crypto-universe] wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
