#!/usr/bin/env python3
"""A3 audit: declared vs source-inferred strategy lookbacks (static regex, no imports).

Run before flipping OPENCLAW_PRICES_TRAILING_WINDOW=1. Report-only by default
(exit 0); pass --fail-on-exceed for a non-zero exit when any strategy exceeds.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

_BAR_PATTERNS = [
    r"rolling\(\s*(?:window\s*=\s*)?(\d+)",
    r"shift\(\s*(\d+)",
    r"pct_change\(\s*(\d+)",
    r"diff\(\s*(\d+)",
    r"iloc\[\s*-\s*(\d+)",
    r"\.tail\(\s*(\d+)",
]
_UNBOUNDED = ["ewm(", "expanding(", "cumsum(", "cumprod(", "cummax(", "cummin("]
_CALL_RE = re.compile(r"(?:rolling|ewm|expanding|shift|pct_change|diff|tail)\(\s*([^)]*)")
_DECL_RE = re.compile(r"max_lookback_days\s*(?::\s*int)?\s*=\s*(\d+)")


def infer_lookback(source: str) -> dict:
    bars = 0
    for pat in _BAR_PATTERNS:
        for m in re.finditer(pat, source):
            bars = max(bars, int(m.group(1)))
    unbounded = [t for t in _UNBOUNDED if t in source]
    non_literal = 0
    for m in re.finditer(r"(?:rolling|shift|pct_change|diff|\.tail)\(\s*([^)]*)", source):
        arg = m.group(1).strip()
        if arg and not re.match(r"(?:window\s*=\s*)?\d+\s*(?:,|$)", arg):
            non_literal += 1
    d = _DECL_RE.search(source)
    return {
        "bars": bars,
        "unbounded": unbounded,
        "non_literal": non_literal,
        "declared": int(d.group(1)) if d else None,
    }


def bars_to_calendar_days(bars: int) -> int:
    return math.ceil(bars * 365 / 252) + 7


def audit(impl_dir: Path, window_days: int = 1095) -> list[dict]:
    rows = []
    for f in sorted(Path(impl_dir).glob("*.py")):
        r = infer_lookback(f.read_text(errors="replace"))
        inferred = bars_to_calendar_days(r["bars"]) if r["bars"] else 0
        rows.append({
            "strategy_file": f.name,
            "declared": r["declared"],
            "inferred_days": inferred,
            "unbounded": r["unbounded"],
            "non_literal": r["non_literal"],
            "exceeds": max(r["declared"] or 0, inferred) > window_days,
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--impl-dir", default="src/strategies/implementations")
    ap.add_argument("--window-days", type=int, default=1095)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-exceed", action="store_true")
    args = ap.parse_args(argv)
    rows = audit(Path(args.impl_dir), args.window_days)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        print(f"{'strategy_file':50} {'declared':>8} {'inferred':>8} {'nonlit':>6} exceeds unbounded")
        for r in rows:
            print(f"{r['strategy_file']:50} {str(r['declared']):>8} {r['inferred_days']:>8} "
                  f"{r['non_literal']:>6} {str(r['exceeds']):7} {','.join(r['unbounded'])}")
    n_exc = sum(r["exceeds"] for r in rows)
    print(f"[lookback_audit] files={len(rows)} exceeds={n_exc} "
          f"unbounded={sum(bool(r['unbounded']) for r in rows)} "
          f"non_literal={sum(r['non_literal'] > 0 for r in rows)} window_days={args.window_days}",
          file=sys.stderr if args.json else sys.stdout)
    return 1 if (args.fail_on_exceed and n_exc) else 0


if __name__ == "__main__":
    sys.exit(main())
