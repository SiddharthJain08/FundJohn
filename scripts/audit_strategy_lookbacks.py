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
# MUST match LOOKBACK_CALENDAR_FACTOR in src/execution/engine.py (not imported:
# the audit never imports engine/strategies; a test pins the two together).
LOOKBACK_CALENDAR_FACTOR = 1.6


def _assign_re(name: str):
    return re.compile(rf"^[ \t]*{name}\s*(?::\s*int)?\s*=\s*([^\s#]+)\s*(?:#.*)?$", re.M)


def _int_assignments(source: str, name: str) -> tuple[list[int], int]:
    """(literal values incl. resolved module constants, count of unresolved symbolic)."""
    vals, sym = [], 0
    for m in _assign_re(name).finditer(source):
        tok = m.group(1)
        if tok.isdigit():
            vals.append(int(tok))
            continue
        c = re.search(rf"^{re.escape(tok)}\s*(?::\s*int)?\s*=\s*(\d+)\s*(?:#.*)?$", source, re.M)
        if c:
            vals.append(int(c.group(1)))
        else:
            sym += 1
    return vals, sym


def _first_arg(source: str, start: int) -> str:
    """Text of the first call argument beginning at `start` (just after '('), paren-balanced."""
    depth, i = 0, start
    while i < len(source):
        ch = source[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return source[start:i].strip()


def infer_lookback(source: str) -> dict:
    bars = 0
    for pat in _BAR_PATTERNS:
        for m in re.finditer(pat, source):
            bars = max(bars, int(m.group(1)))
    unbounded = [t for t in _UNBOUNDED if t in source]
    non_literal = 0
    for m in re.finditer(r"(?:rolling|shift|pct_change|diff|\.tail)\(", source):
        arg = _first_arg(source, m.end())
        if arg and not re.fullmatch(r"(?:window\s*=\s*)?\d+", arg):
            non_literal += 1
    decl, decl_sym = _int_assignments(source, "max_lookback_days")
    minl, minl_sym = _int_assignments(source, "min_lookback")
    non_literal += decl_sym + minl_sym
    return {
        "bars": bars,
        "unbounded": unbounded,
        "non_literal": non_literal,
        "declared": max(decl) if decl else None,
        "min_lookback": max(minl) if minl else None,
    }


def bars_to_calendar_days(bars: int) -> int:
    return math.ceil(bars * 365 / 252) + 7


def audit(impl_dir: Path, window_days: int = 1095) -> list[dict]:
    rows = []
    for f in sorted(Path(impl_dir).glob("*.py")):
        r = infer_lookback(f.read_text(errors="replace"))
        inferred = bars_to_calendar_days(r["bars"]) if r["bars"] else 0
        need = max(r["declared"] or 0, math.ceil((r["min_lookback"] or 0) * LOOKBACK_CALENDAR_FACTOR))
        rows.append({
            "strategy_file": f.name,
            "declared": r["declared"],
            "min_lookback": r["min_lookback"],
            "engine_need_days": need,
            "inferred_days": inferred,
            "unbounded": r["unbounded"],
            "non_literal": r["non_literal"],
            "exceeds": max(need, inferred) > window_days,
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--impl-dir", default="src/strategies/implementations")
    ap.add_argument("--window-days", type=int, default=1095)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-exceed", action="store_true")
    ap.add_argument("--fail-on-unverified", action="store_true",
                    help="with --fail-on-exceed: also fail on non-literal/unbounded files")
    args = ap.parse_args(argv)
    rows = audit(Path(args.impl_dir), args.window_days)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        print(f"{'strategy_file':50} {'declared':>8} {'min_lb':>6} {'need':>6} {'inferred':>8} {'nonlit':>6} exceeds unbounded")
        for r in rows:
            print(f"{r['strategy_file']:50} {str(r['declared']):>8} {str(r['min_lookback']):>6} {r['engine_need_days']:>6} {r['inferred_days']:>8} "
                  f"{r['non_literal']:>6} {str(r['exceeds']):7} {','.join(r['unbounded'])}")
    unver = [r for r in rows if r["non_literal"] or r["unbounded"]]
    if not args.json:
        print("UNVERIFIED (non-literal windows / unbounded ewm,cum*; review manually):")
        for r in unver:
            print(f"  {r['strategy_file']}: non_literal={r['non_literal']} unbounded={','.join(r['unbounded'])}")
    n_exc = sum(r["exceeds"] for r in rows)
    print(f"[lookback_audit] files={len(rows)} exceeds={n_exc} "
          f"unbounded={sum(bool(r['unbounded']) for r in rows)} "
          f"non_literal={sum(r['non_literal'] > 0 for r in rows)} window_days={args.window_days}",
          file=sys.stderr if args.json else sys.stdout)
    if args.fail_on_exceed and (n_exc or (args.fail_on_unverified and unver)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
