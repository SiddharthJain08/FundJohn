#!/usr/bin/env python3
"""
repair_manifest_canonical_files.py — case-toggle repair for manifest
canonical_file drift.

Background — see system_check `manifest_canonical_file_consistency`. When
strategycoder records the wrong filename case in metadata.canonical_file
(template drift), the orchestrator's `_resolveImplPath` returns a missing
path and the validator quarantines the strategy as `validation_failed`. This
script scans every manifest entry, and for any whose canonical_file is
missing on disk but exists case-toggled, rewrites it to the actual filename.

Usage:
    python3 scripts/repair_manifest_canonical_files.py [--dry-run]

Exit codes:
    0 — no drift detected (or all repaired)
    1 — truly-missing files found (require manual investigation; canonical_file
        points at a basename that doesn't exist in either case)
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'

DRY_RUN = '--dry-run' in sys.argv


def main():
    if not MANIFEST.is_file():
        print(f'[repair] no manifest at {MANIFEST}', file=sys.stderr)
        return 1

    m = json.loads(MANIFEST.read_text())
    fixed = []
    truly_missing = []

    for sid, rec in (m.get('strategies') or {}).items():
        md = rec.get('metadata') or {}
        cf = md.get('canonical_file')
        if not cf:
            continue
        if (IMPL_DIR / cf).is_file():
            continue
        # File doesn't exist as recorded — try case-toggled basename
        toggled = cf[0].swapcase() + cf[1:]
        if (IMPL_DIR / toggled).is_file():
            fixed.append((sid, cf, toggled))
            if not DRY_RUN:
                md['canonical_file'] = toggled
        else:
            truly_missing.append((sid, cf))

    if not fixed and not truly_missing:
        print(f'[repair] all canonical_file entries resolve cleanly — no drift detected')
        return 0

    print(f'[repair] drift detected:')
    print(f'  case-drift (repairable): {len(fixed)}')
    for sid, old, new in fixed[:20]:
        print(f'    {sid:50}  {old!r} → {new!r}')
    if len(fixed) > 20:
        print(f'    ... +{len(fixed) - 20} more')
    if truly_missing:
        print(f'  truly-missing: {len(truly_missing)}')
        for sid, cf in truly_missing[:20]:
            print(f'    {sid:50}  {cf!r}')

    if DRY_RUN:
        print('[repair] DRY-RUN — no changes written')
        return 1 if truly_missing else 0

    if fixed:
        # Snapshot before mutating (timestamped, append-only filesystem alongside the live manifest)
        stamp = datetime.utcnow().strftime('%Y-%m-%dT%H%M%S')
        backup = MANIFEST.with_suffix(f'.json.bak-{stamp}-canonicalfile-repair')
        shutil.copy(MANIFEST, backup)
        MANIFEST.write_text(json.dumps(m, indent=2) + '\n')
        print(f'[repair] wrote {len(fixed)} fixes to {MANIFEST}')
        print(f'[repair] backup: {backup}')

    return 1 if truly_missing else 0


if __name__ == '__main__':
    sys.exit(main())
