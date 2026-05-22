#!/usr/bin/env python3
"""CI gate: scan src/strategies/implementations/*.py + universe_default.py."""
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `src.*` imports resolve whether
# this script is run from the repo root or via python3 directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.strategies.universe_lint import scan_module

ROOTS = [
    Path("src/strategies/universe_default.py"),
    *Path("src/strategies/implementations").rglob("S*.py"),
]

def main() -> int:
    total = 0
    for p in ROOTS:
        if not p.exists(): continue
        for e in scan_module(p):
            print(f"{e.path}:{e.line} {e.message}")
            total += 1
    if total:
        print(f"\n{total} predicate lint error(s)")
        return 1
    print(f"scanned {len(ROOTS)} files — clean")
    return 0

if __name__ == "__main__":
    sys.exit(main())
