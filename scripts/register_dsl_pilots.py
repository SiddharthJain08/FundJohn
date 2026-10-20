#!/usr/bin/env python3
"""
register_dsl_pilots.py - operator script registering the three C7 DSL pilots
as `candidate` entries in manifest.json (six-repo adoptions, Stream C, Task 10):

    S_golden_cross_trend_signal_tv2_dsl, S_amihud_illiquidity_premium_dsl,
    oxf_bollinger_momentum_dsl

Same precedent as scripts/register_low_volatility_us_gk63.py: the production
manifest.json is live and rewritten by LifecycleStateMachine /
src/strategies/_manifest_lock.py on main, so it is NEVER hand-edited on a
branch. Run `--apply` against main's manifest AFTER the stream merges.

Inserts only the three new keys (state=candidate, eligible_regimes ABSENT -
the eligibility assigner sets it from the first backtest) and never touches
any other key. Every entry carries `universe_filter_ref`: an unbound candidate
gets the full ~12.5k-column panel and the DSL builds several full-prefix wide
intermediates per bar (S_first_candle_ema12_breakout was OOM-killed three
nights running for this reason).
  - Amihud copies its parent (stocks_liquid + backtest_universe_cap tier_liquid
    and the parent's cap reason).
  - Bollinger copies its parent (tier_liquid).
  - Golden cross: the tv2 parent has NO manifest entry, so tier_liquid (the
    modal fleet predicate) is used. OWED OPERATOR DECISION: confirm or change.

Usage:
    python3 scripts/register_dsl_pilots.py [--manifest PATH] [--dry-run]
    python3 scripts/register_dsl_pilots.py [--manifest PATH] --apply

--dry-run (default) writes nothing and takes no lock. POSTGRES_URI is popped
for the whole call (DB-free in both modes). --apply is one locked
read-modify-write via _manifest_lock.write_atomic; if every key is already
present it is a pure no-op (write_atomic never called, so a non-byte-stable
live manifest is not re-serialized for nothing). Idempotent.
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

TIER_LIQUID = 'src.strategies.universe_default:tier_liquid'
STOCKS_LIQUID = 'src.strategies.universe_default:stocks_liquid'

AMIHUD_CAP_REASON = ('OOM 2x on full 12.5k-col panel (2026-07-28 honest-cost epoch, nights 1); '
                     'bounded run is the shrink baseline (hurst/bocpd precedent)')

PILOTS = {
    'S_golden_cross_trend_signal_tv2_dsl': {
        'canonical_file': 'S_golden_cross_trend_signal_tv2_dsl.py',
        'class': 'GoldenCrossTrendStateTV2Dsl',
        'description': 'Golden-cross trend state (SMA50 > SMA200) as a SIGNAL formula; DSL twin of S_golden_cross_trend_signal_tv2',
        'universe_filter_ref': TIER_LIQUID,
        'instrument_class': 'equity',
    },
    'S_amihud_illiquidity_premium_dsl': {
        'canonical_file': 'S_amihud_illiquidity_premium_dsl.py',
        'class': 'AmihudIlliquidityPremiumDsl',
        'description': 'Amihud ILLIQ 60d rank within the liquid pool as a SIGNAL formula; DSL twin of S_amihud_illiquidity_premium',
        'universe_filter_ref': STOCKS_LIQUID,
        'backtest_universe_cap': 'tier_liquid',
        'backtest_universe_cap_reason': AMIHUD_CAP_REASON,
        'instrument_class': 'equity',
    },
    'oxf_bollinger_momentum_dsl': {
        'canonical_file': 'oxf_bollinger_momentum_dsl.py',
        'class': 'OxfBollingerMomentumDsl',
        'description': 'Bollinger-band momentum breakout on liquid ETFs as a SIGNAL formula; DSL twin of oxf_bollinger_momentum',
        'universe_filter_ref': TIER_LIQUID,
        'instrument_class': 'etp',
    },
}
IDS = tuple(PILOTS)


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def _new_entry(sid: str, state_since: str) -> dict:
    spec = PILOTS[sid]
    meta = {k: v for k, v in spec.items() if k != 'instrument_class'}
    return {
        'state': 'candidate',
        'state_since': state_since,
        'metadata': meta,
        'history': [],
        'instrument_class': spec['instrument_class'],
    }


def _diff_outside_targets(before: dict, after: dict) -> list[str]:
    changed = []
    b = (before or {}).get('strategies', {}) or {}
    a = (after or {}).get('strategies', {}) or {}
    for sid, entry in b.items():
        if sid not in IDS and a.get(sid) != entry:
            changed.append(sid)
    for sid in a:
        if sid not in b and sid not in IDS:
            changed.append(f'<unexpected new strategy: {sid}>')
    for key in ('schema_version', 'decommissioned'):
        if (before or {}).get(key) != (after or {}).get(key):
            changed.append(f'<top-level key changed: {key}>')
    return changed


def run(manifest_path: Path, apply: bool) -> int:
    tag = '[register_dsl_pilots]'
    if not manifest_path.is_file():
        print(f'{tag} no manifest at {manifest_path}', file=sys.stderr)
        return 1
    saved = os.environ.pop('POSTGRES_URI', None)
    try:
        if not apply:
            strategies = json.loads(manifest_path.read_text(encoding='utf-8')).get('strategies') or {}
            print(f'{tag} DRY-RUN - no changes written. manifest={manifest_path}')
            for sid in IDS:
                if sid in strategies:
                    print(f'{tag} {sid} already present - --apply would skip it.')
                else:
                    print(f'{tag} would insert {sid} (state_since is a preview):')
                    print(json.dumps(_new_entry(sid, _now_iso()), indent=2))
            return 0

        with _ml.manifest_lock(manifest_path, actor='stream-c:register-dsl-pilots'):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                disk = json.load(f)
            strategies = disk.setdefault('strategies', {})
            missing = [sid for sid in IDS if sid not in strategies]
            if not missing:
                print(f'{tag} APPLY: no-op - all three pilots already present.')
                return 0
            before = copy.deepcopy(disk)
            ts = _now_iso()
            for sid in missing:
                strategies[sid] = _new_entry(sid, ts)
            unsafe = _diff_outside_targets(before, disk)
            if unsafe:
                print(f'{tag} APPLY REFUSED: entries changed outside the pilots: {unsafe}',
                      file=sys.stderr)
                return 1
            _ml.write_atomic(manifest_path, disk)
            on_disk = json.loads(manifest_path.read_text(encoding='utf-8'))
            if on_disk != disk:
                print(f'{tag} APPLY REFUSED: read-back mismatch - manifest may be corrupted.',
                      file=sys.stderr)
                return 1
        print(f'{tag} APPLY complete - inserted {", ".join(missing)} as candidate.')
        return 0
    finally:
        if saved is not None:
            os.environ['POSTGRES_URI'] = saved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Register the three C7 DSL pilots as candidates.')
    parser.add_argument('--manifest', default=str(DEFAULT_MANIFEST))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Preview only (default).')
    mode.add_argument('--apply', action='store_true', help='Write under the manifest lock.')
    args = parser.parse_args(argv)
    return run(Path(args.manifest), apply=bool(args.apply))


if __name__ == '__main__':
    sys.exit(main())
