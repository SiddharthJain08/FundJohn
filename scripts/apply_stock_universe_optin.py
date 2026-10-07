#!/usr/bin/env python3
"""
apply_stock_universe_optin.py — operator script for the Phase 2 stock-universe
opt-in (spec docs/specs/2026-10-04-universe-security-type-spec.md §2).

Reads the operator-approved list (`strategy_id<TAB>ref`, `#` comments) and, for
each listed strategy, points `metadata.universe_filter_ref` at the target
predicate (the 21-strategy list moves them to `stocks_liquid`). Modelled on
scripts/register_low_volatility_us_gk63.py and
scripts/apply_qd_manifest_hygiene.py: dry-run is the DEFAULT, --apply does ONE
read-modify-write under the cross-process manifest lock
(`_manifest_lock.manifest_lock` + `write_atomic`), POSTGRES_URI is popped for
the whole call (this script never needs a DB), and a no-op --apply never calls
write_atomic.

Usage:
    python3 scripts/apply_stock_universe_optin.py [--list PATH] [--manifest PATH] [--dry-run]
    python3 scripts/apply_stock_universe_optin.py [--list PATH] [--manifest PATH] \
        [--audit-out PATH] --apply

VALIDATION (the WHOLE run is refused — non-zero exit, nothing written — on any
failure): every listed strategy exists in the manifest; every ref is
`module:name` with `name` in LADDER_TIER_PREDICATES | CANDIDATE_PREDICATES AND
resolvable through the same importlib.import_module + getattr path
UniverseResolver._load_predicate uses; no strategy id appears twice in the list.

PER-STRATEGY CHANGE: if metadata.universe_filter_ref already equals the target
the strategy is a no-op. Otherwise four metadata keys change, nothing else:
  1. metadata.universe_filter_ref             = target
  2. metadata.universe_filter_ref_prior       = previous value, or the literal
     "<none>" (an existing _prior from an earlier run is KEPT — the first
     prior is the one worth restoring)
  3. metadata.universe_filter_ref_changed_at  = apply-time UTC ms-'Z' stamp
  4. metadata.universe_filter_ref_changed_by  = 'manual:operator'
     (3 and 4 are overwritten on every real change.)
NO history event is appended: strategy_weights._strategies_in_grace_period reads
the latest history event with to_state in {live, monitoring} as a promotion, so
a same-state event would silently put the strategy into the 30-day auto-demote
grace window. Provenance lives in metadata only; `history` is untouched.

--apply asserts, after writing and re-reading, that the file is valid JSON,
that every entry NOT in the list is identical, and that listed entries differ
only in the four metadata keys above (history unchanged). It writes an audit JSON
{strategy_id: {prior, new, changed}} (default <list dir>/phase2-optin-audit.json)
only when something changed, so a no-op re-run never clobbers the first run's
audit. Idempotent: a second --apply changes nothing and does not rewrite the
manifest (the real manifest is not byte-stable under write_atomic — see the
NON-ASCII NOTE in apply_qd_manifest_hygiene.py — so even a "same content"
write would churn bytes; the FIRST real --apply WILL re-escape non-ASCII
characters elsewhere in the file, with every parsed value unchanged).
"""
from __future__ import annotations

import argparse
import copy
import difflib
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
DEFAULT_LIST = (ROOT / '.superpowers' / 'sdd' / '2026-10-04-universe-security-type'
                / 'phase2-optin-list.txt')
AUDIT_NAME = 'phase2-optin-audit.json'

# ROOT first so the `src.strategies.universe_default:name` refs resolve exactly
# as they do in the resolver (which runs with the repo root on sys.path).
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
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

ACTOR = 'manual:operator'
SPEC_REF = 'docs/specs/2026-10-04-universe-security-type-spec.md'
NONE_PRIOR = '<none>'
PROVENANCE_KEYS = ('universe_filter_ref', 'universe_filter_ref_prior',
                   'universe_filter_ref_changed_at', 'universe_filter_ref_changed_by')
LOCK_ACTOR = 'universe-security-type:phase2-optin'
TAG = '[apply_stock_universe_optin]'

CAVEAT = (
    "CAVEAT: a live strategy's universe changes on its next signals run "
    "(weekday 19:00Z); backtests only change in the Phase 3 epoch; the "
    "picker/activation may change on the next activation step."
)


class RefusedError(RuntimeError):
    """Validation or integrity failure — the whole run is refused."""


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def parse_list(path: Path) -> list[tuple[str, str]]:
    """`strategy_id<TAB>ref` per line; blank lines and `#` comments skipped."""
    out: list[tuple[str, str]] = []
    for n, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('\t') if p.strip()]
        if len(parts) != 2:
            raise RefusedError(f"{path}:{n}: expected 'strategy_id<TAB>ref', got {raw!r}")
        out.append((parts[0], parts[1]))
    return out


def _allowed_names() -> set[str]:
    ud = importlib.import_module('src.strategies.universe_default')
    return set(ud.LADDER_TIER_PREDICATES) | set(ud.CANDIDATE_PREDICATES)


def validate(items: list[tuple[str, str]], strategies: dict) -> None:
    """Raise RefusedError listing EVERY problem found (not just the first)."""
    problems: list[str] = []
    seen: set[str] = set()
    allowed = None
    for sid, ref in items:
        if sid in seen:
            problems.append(f"{sid}: duplicate strategy id in the list")
        seen.add(sid)
        if sid not in strategies:
            problems.append(f"{sid}: not present in the manifest")
        if ':' not in ref:
            problems.append(f"{sid}: ref {ref!r} is not 'module:name'")
            continue
        mod_path, attr = ref.rsplit(':', 1)
        if allowed is None:
            allowed = _allowed_names()
        if attr not in allowed:
            problems.append(f"{sid}: predicate {attr!r} is not in "
                            f"LADDER_TIER_PREDICATES | CANDIDATE_PREDICATES")
            continue
        try:  # same resolution path as UniverseResolver._load_predicate
            fn = getattr(importlib.import_module(mod_path), attr)
        except Exception as exc:
            problems.append(f"{sid}: ref {ref!r} does not resolve ({type(exc).__name__}: {exc})")
            continue
        if not callable(fn):
            problems.append(f"{sid}: ref {ref!r} resolves to a non-callable")
    if not items:
        problems.append("the list contains no strategies")
    if problems:
        raise RefusedError('; '.join(problems))


def plan_changes(items: list[tuple[str, str]], strategies: dict, now: str):
    """Mutate `strategies` in place. Returns {sid: {prior, new, changed}}."""
    audit: dict[str, dict] = {}
    for sid, target in items:
        entry = strategies[sid]
        meta = entry.setdefault('metadata', {})
        current = meta.get('universe_filter_ref')
        if current == target:
            audit[sid] = {'prior': meta.get('universe_filter_ref_prior'),
                          'new': target, 'changed': False}
            continue
        prior = current if current is not None else NONE_PRIOR
        meta['universe_filter_ref'] = target
        if 'universe_filter_ref_prior' not in meta:   # keep the FIRST prior
            meta['universe_filter_ref_prior'] = prior
        meta['universe_filter_ref_changed_at'] = now
        meta['universe_filter_ref_changed_by'] = ACTOR
        audit[sid] = {'prior': prior, 'new': target, 'changed': True}
    return audit


def check_only_expected_changed(before: dict, after: dict, audit: dict) -> list[str]:
    """Problems (empty == safe): entries outside the list identical; listed
    entries differ only in the four PROVENANCE_KEYS metadata keys (history must
    be unchanged); top level untouched."""
    bad: list[str] = []
    b = (before or {}).get('strategies', {}) or {}
    a = (after or {}).get('strategies', {}) or {}
    if set(a) != set(b):
        bad.append('<strategy set changed>')
    for key in set(before) | set(after):
        if key != 'strategies' and before.get(key) != after.get(key):
            bad.append(f'<top-level key changed: {key}>')
    for sid, be in b.items():
        ae = a.get(sid)
        if sid not in audit:
            if ae != be:
                bad.append(sid)
            continue
        if ae is None:
            bad.append(sid)
            continue
        be2, ae2 = copy.deepcopy(be), copy.deepcopy(ae)
        for e in (be2, ae2):
            m = e.setdefault('metadata', {})
            for k in PROVENANCE_KEYS:
                m.pop(k, None)
        if ae2 != be2:  # includes history: it must be byte-for-byte unchanged
            bad.append(sid)
    return bad


def _byte_stable(text: str) -> bool:
    return json.dumps(json.loads(text), indent=2) == text


def _print_plan(audit: dict) -> None:
    for sid, a in audit.items():
        if a['changed']:
            print(f"  {sid}: {a['prior']} -> {a['new']}")
        else:
            print(f"  {sid}: no-op (already {a['new']})")


def run(list_path: Path, manifest_path: Path, audit_out: Path, apply: bool) -> int:
    if not manifest_path.is_file():
        print(f"{TAG} no manifest at {manifest_path}", file=sys.stderr)
        return 1
    if not list_path.is_file():
        print(f"{TAG} no list at {list_path}", file=sys.stderr)
        return 1
    _saved_pguri = os.environ.pop('POSTGRES_URI', None)
    try:
        items = parse_list(list_path)
        if not apply:
            text = manifest_path.read_text(encoding='utf-8')
            original = json.loads(text)
            bs = _byte_stable(text)
            print(f"{TAG} byte-stable under write_atomic: {bs}"
                  + ('' if bs else
                     " (expected if the manifest has literal non-ASCII characters written by a "
                     "JS caller — see NON-ASCII NOTE in apply_qd_manifest_hygiene.py; the FIRST "
                     "real --apply re-escapes them into \\uXXXX, so the byte diff will be far "
                     "larger than the listed strategies even though every other entry's PARSED "
                     "value is unchanged)"))
            validate(items, original.get('strategies') or {})
            preview = copy.deepcopy(original)
            audit = plan_changes(items, preview['strategies'], _now_iso())
            bad = check_only_expected_changed(original, preview, audit)
            if bad:
                raise RefusedError(f"internal check failed: unexpected changes in {bad}")
            n = sum(1 for a in audit.values() if a['changed'])
            print(f"{TAG} DRY-RUN — no changes written. manifest={manifest_path} "
                  f"list={list_path} changed={n} no-op={len(audit) - n}")
            _print_plan(audit)
            if n == 0:
                print(f"{TAG} --apply would be a no-op.")
            else:
                print(f"{TAG} confirmed: only metadata.universe_filter_ref / "
                      f"_prior / _changed_at / _changed_by change on the "
                      f"listed strategies (history untouched); every other entry is identical. "
                      f"(_changed_at in the diff is a preview — --apply stamps the real time.)")
                print()
                sys.stdout.writelines(difflib.unified_diff(
                    text.splitlines(keepends=True),
                    json.dumps(preview, indent=2).splitlines(keepends=True),
                    fromfile=f"{manifest_path} (current)",
                    tofile=f"{manifest_path} (after --apply)",
                ))
                print()
            print(CAVEAT)
            return 0

        with _ml.manifest_lock(manifest_path, actor=LOCK_ACTOR):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                disk = json.load(f)
            validate(items, disk.get('strategies') or {})
            before = copy.deepcopy(disk)
            audit = plan_changes(items, disk['strategies'], _now_iso())
            bad = check_only_expected_changed(before, disk, audit)
            if bad:
                raise RefusedError(f"internal check failed, refusing to write: {bad}")
            n = sum(1 for a in audit.values() if a['changed'])
            if n == 0:
                print(f"{TAG} APPLY: no-op — every listed strategy already has its target ref "
                      f"(manifest not rewritten, audit not touched).")
                _print_plan(audit)
                print(CAVEAT)
                return 0
            _ml.write_atomic(manifest_path, disk)
            on_disk = json.loads(manifest_path.read_text(encoding='utf-8'))
            if on_disk != disk:
                raise RefusedError("post-write read-back does not match the intended payload — "
                                   "manifest may be corrupted, investigate manually")
            bad = check_only_expected_changed(before, on_disk, audit)
            if bad:
                raise RefusedError(f"post-write read-back check failed: {bad}")
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        audit_out.write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
        print(f"{TAG} APPLY complete — changed={n} no-op={len(audit) - n}; audit -> {audit_out}")
        _print_plan(audit)
        print(CAVEAT)
        return 0
    except RefusedError as exc:
        print(f"{TAG} {'APPLY' if apply else 'DRY-RUN — would'} REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        if _saved_pguri is not None:
            os.environ['POSTGRES_URI'] = _saved_pguri


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=f"Phase 2 stock-universe opt-in (spec {SPEC_REF} §2): set "
                    f"metadata.universe_filter_ref for the operator-approved list.")
    p.add_argument('--list', default=str(DEFAULT_LIST),
                   help=f"Opt-in list (default: {DEFAULT_LIST})")
    p.add_argument('--manifest', default=str(DEFAULT_MANIFEST),
                   help=f"Path to manifest.json (default: {DEFAULT_MANIFEST})")
    p.add_argument('--audit-out', default=None,
                   help=f"Audit JSON path (default: <list dir>/{AUDIT_NAME})")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Preview only (default).')
    mode.add_argument('--apply', action='store_true',
                      help='Write, through the cross-process manifest lock.')
    a = p.parse_args(argv)
    lp = Path(a.list)
    audit_out = Path(a.audit_out) if a.audit_out else lp.parent / AUDIT_NAME
    return run(lp, Path(a.manifest), audit_out, apply=bool(a.apply))


if __name__ == '__main__':
    sys.exit(main())
