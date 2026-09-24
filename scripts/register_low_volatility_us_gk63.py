#!/usr/bin/env python3
"""
register_low_volatility_us_gk63.py — operator script for D Task 9 registration
of S_low_volatility_us_gk63 into manifest.json as a `candidate` entry (spec
docs/specs/2026-09-12-quantdinger-adoptions-spec.md §4 D4, item 9).

COORDINATOR RULING (amendment, branch worktree-qd-adoptions, 2026-09-24
01:4x UTC): production manifest.json is live and continuously rewritten by
LifecycleStateMachine + src/strategies/_manifest_lock.py::write_atomic on
main (same ruling as Stream A Task 6's scripts/apply_qd_manifest_hygiene.py),
so it is NOT hand-edited on this branch. This script is the deferred
operator action that performs the registration for real — run it against
main's manifest AFTER the wave-2 merge and BEFORE the next Saturday sweep
(never on the branch itself).

What it does: inserts exactly one new key, S_low_volatility_us_gk63, into
manifest['strategies'] as a fresh `candidate` (state_since = the real
apply-time UTC timestamp; eligible_regimes deliberately absent — the
eligibility assigner sets it from the first backtest's per-regime metrics,
same as every other freshly-minted candidate). It never transitions an
existing entry, never goes through LifecycleStateMachine (there is nothing
to transition — this is a brand-new key, not a state change), and never
touches any other key in the manifest.

Usage:
    python3 scripts/register_low_volatility_us_gk63.py [--manifest PATH] [--dry-run]
    python3 scripts/register_low_volatility_us_gk63.py [--manifest PATH] --apply

--dry-run is the DEFAULT: reads the manifest, prints whether
S_low_volatility_us_gk63 is already present, prints the exact candidate
entry it would insert (or the existing entry, if already present), and
writes nothing — no lock is taken, no file is touched. POSTGRES_URI is
popped from the environment for the ENTIRE call — both --dry-run and
--apply (fix round 1, R5: this script never needs a DB connection in either
mode) — restored in a `finally`, so neither mode is DB-free merely by
convention — belt-and-suspenders: this script imports only
strategies._manifest_lock, which never opens a Postgres connection, but the
hygiene script's precedent (scripts/apply_qd_manifest_hygiene.py) is to pop
it unconditionally in case that ever changes.

Also prints whether the on-disk manifest is byte-stable under
json.dumps(..., indent=2) (see the NON-ASCII NOTE in
apply_qd_manifest_hygiene.py — the live manifest is written by a JS caller
that does not re-escape non-ASCII characters the way Python's
json.dumps(ensure_ascii=True) does, so it is routinely NOT byte-stable; that
is exactly why --apply below is written to avoid ever re-serializing the
WHOLE file when nothing needs to change).

--apply performs the real read-modify-write under
strategies._manifest_lock.manifest_lock (the same cross-process lock JS
writers — saturday_brain.js, the finisher, approvals — use). If
S_low_volatility_us_gk63 is already present it is a pure no-op: the lock is
taken and released but write_atomic is never called, so a non-byte-stable
live manifest is never rewritten for nothing (a naive "always
write_atomic(disk)" no-op would otherwise re-serialize ~180 escaped
non-ASCII characters elsewhere in the file into ASCII-safe \\uXXXX escapes on
every run, producing large byte-level diff noise unrelated to this
strategy). Only when inserting for the first time does it write, through
write_atomic (json.dumps(indent=2), no trailing newline, atomic
write-then-rename) — and it then reads the file back and asserts the
result is valid JSON matching the intended in-memory payload (the write
must not have corrupted the file).

Idempotent: running --apply twice in a row leaves the file byte-identical
the second time (state_since is set once, on first insertion, and never
touched again by this script). It never changes the value of any manifest
entry other than S_low_volatility_us_gk63.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'

sys.path.insert(0, str(ROOT / 'src'))
try:
    from strategies import _manifest_lock as _ml
except ImportError:
    import importlib.util

    def _load(name: str, relpath: str):
        spec = importlib.util.spec_from_file_location(name, str(ROOT / relpath))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    _ml = _load('strategies._manifest_lock', 'src/strategies/_manifest_lock.py')


STRATEGY_ID = 'S_low_volatility_us_gk63'
SPEC_REF = 'docs/specs/2026-09-12-quantdinger-adoptions-spec.md'

DESCRIPTION = (
    'Garman-Klass range-vol variant of low_volatility_us: rank asc by mean '
    '63-session Garman-Klass variance; LONG the lowest-variance decile, equal-weight'
)


def _now_iso() -> str:
    """UTC timestamp in the manifest's own convention, e.g.
    '2026-04-30T15:02:14.260Z' (milliseconds, trailing 'Z', not '+00:00')."""
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def _new_entry(state_since: str) -> dict:
    return {
        'state': 'candidate',
        'state_since': state_since,
        'metadata': {
            'canonical_file': 'S_low_volatility_us_gk63.py',
            'class': 'LowVolatilityUSGK63',
            'description': DESCRIPTION,
            'universe_filter_ref': 'src.strategies.universe_default:tier_r3000',
        },
        'history': [],
        'instrument_class': 'equity',
    }


def _byte_stable(text: str) -> bool:
    """True iff re-serializing the parsed manifest through write_atomic's
    exact convention (json.dumps(indent=2), no trailing newline) reproduces
    `text` byte-for-byte. See apply_qd_manifest_hygiene.py's NON-ASCII NOTE
    — commonly False on the real manifest for a known, benign reason."""
    return json.dumps(json.loads(text), indent=2) == text


def _diff_outside_target(before: dict, after: dict) -> list[str]:
    """Return keys/strategies that changed VALUE outside STRATEGY_ID — empty
    means safe. Belt-and-suspenders: this script is only ever supposed to
    insert strategies[STRATEGY_ID] and leave everything else alone."""
    changed = []
    b_strats = (before or {}).get('strategies', {}) or {}
    a_strats = (after or {}).get('strategies', {}) or {}
    for sid, b_entry in b_strats.items():
        if sid == STRATEGY_ID:
            continue
        if a_strats.get(sid) != b_entry:
            changed.append(sid)
    for sid in a_strats:
        if sid not in b_strats and sid != STRATEGY_ID:
            changed.append(f'<unexpected new strategy: {sid}>')
    for key in ('schema_version', 'decommissioned'):
        if (before or {}).get(key) != (after or {}).get(key):
            changed.append(f'<top-level key changed: {key}>')
    return changed


def run(manifest_path: Path, apply: bool) -> int:
    if not manifest_path.is_file():
        print(f"[register_low_volatility_us_gk63] no manifest at {manifest_path}", file=sys.stderr)
        return 1

    # This script never needs a DB connection in EITHER mode (fix round 1,
    # R5) — pop POSTGRES_URI for the whole call, not just the dry-run branch,
    # so --apply is DB-free by construction too, restored in a `finally`.
    _saved_pguri = os.environ.pop('POSTGRES_URI', None)
    try:
        if not apply:
            original_text = manifest_path.read_text(encoding='utf-8')
            original = json.loads(original_text)
            byte_stable = _byte_stable(original_text)
            print(f"[register_low_volatility_us_gk63] byte-stable under write_atomic: {byte_stable}"
                  + ('' if byte_stable else
                     " (expected if the manifest has literal non-ASCII characters written by a "
                     "JS caller — see NON-ASCII NOTE in apply_qd_manifest_hygiene.py. This is "
                     "exactly why a no-op --apply never rewrites the file — but the FIRST real "
                     "--apply, the one that actually inserts this entry, still calls write_atomic "
                     "once and WILL re-escape every such character elsewhere in the file into "
                     "\\uXXXX, producing a real diff far larger than the one new strategy entry "
                     "even though every other entry's PARSED value is unchanged — same caveat the "
                     "hygiene script's dry-run gives for its own first apply)"))

            strategies = original.get('strategies') or {}
            already = STRATEGY_ID in strategies
            preview_entry = strategies.get(STRATEGY_ID) if already else _new_entry(_now_iso())

            print(f"[register_low_volatility_us_gk63] DRY-RUN — no changes written. "
                  f"manifest={manifest_path} already_exists={already}")
            if already:
                print(f"[register_low_volatility_us_gk63] {STRATEGY_ID} is already present "
                      f"— --apply would be a no-op. Existing entry:")
            else:
                print(f"[register_low_volatility_us_gk63] Would insert (state_since below is a "
                      f"preview computed now — the real --apply sets it to the actual apply-time "
                      f"timestamp, which will differ slightly):")
            print(json.dumps(preview_entry, indent=2))
            return 0

        # --apply: single locked read-modify-write. Only writes when actually
        # inserting — a no-op never calls write_atomic (see module docstring).
        with _ml.manifest_lock(manifest_path, actor='qd-stream-d:register-gk63'):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                disk = json.load(f)

            strategies = disk.setdefault('strategies', {})
            if STRATEGY_ID in strategies:
                print(f"[register_low_volatility_us_gk63] APPLY: no-op — {STRATEGY_ID} already present.")
                return 0

            before = copy.deepcopy(disk)
            strategies[STRATEGY_ID] = _new_entry(_now_iso())

            unsafe = _diff_outside_target(before, disk)
            if unsafe:
                # Defensive only — the mutation above only ever assigns
                # strategies[STRATEGY_ID], so this should be unreachable. Refuse
                # rather than write if it ever isn't.
                print(f"[register_low_volatility_us_gk63] APPLY REFUSED: internal check failed, "
                      f"entries changed outside {STRATEGY_ID}: {unsafe}", file=sys.stderr)
                return 1

            _ml.write_atomic(manifest_path, disk)

            # JSON-validity assertion: read the file back and confirm it parses
            # to exactly the payload we intended to write — the write must not
            # have corrupted the file.
            on_disk = json.loads(manifest_path.read_text(encoding='utf-8'))
            if on_disk != disk:
                print(f"[register_low_volatility_us_gk63] APPLY REFUSED: post-write read-back does "
                      f"not match the intended payload — manifest may be corrupted, investigate "
                      f"manually.", file=sys.stderr)
                return 1

        print(f"[register_low_volatility_us_gk63] APPLY complete — inserted {STRATEGY_ID} as candidate.")
        return 0
    finally:
        if _saved_pguri is not None:
            os.environ['POSTGRES_URI'] = _saved_pguri


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"D Task 9 registration: insert {STRATEGY_ID} into manifest.json as a "
            f"fresh 'candidate' entry (spec {SPEC_REF} §4 D4, item 9)."
        ),
    )
    parser.add_argument('--manifest', default=str(DEFAULT_MANIFEST),
                        help=f"Path to manifest.json (default: {DEFAULT_MANIFEST})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='Preview only, write nothing (default).')
    mode.add_argument('--apply', action='store_true',
                      help='Perform the write, through the cross-process manifest lock.')
    args = parser.parse_args(argv)
    return run(Path(args.manifest), apply=bool(args.apply))


if __name__ == '__main__':
    sys.exit(main())
