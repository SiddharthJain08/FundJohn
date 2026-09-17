#!/usr/bin/env python3
"""
apply_qd_manifest_hygiene.py — operator script for Stream A Task 6 manifest
hygiene (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md §0/§1,
A4 hygiene).

COORDINATOR RULING (branch worktree-qd-adoptions, 2026-09-17): the
production `manifest.json` is live and continuously rewritten by
LifecycleStateMachine + src/strategies/_manifest_lock.py::write_atomic on
main, so it is NOT hand-edited on that branch. This script is the deferred
operator action that performs those manifest edits for real, through the
state machine and its cross-process lock — run it against main's manifest
AFTER the branch merges (never on the branch itself).

What it does, for S_TR04_zarattini_intraday_spy and
S_TR06_baltussen_eod_reversal:

  1. Corrects the false 2026-04-30 archival reason ("Requires prices_30m
     which is no longer collected daily") — data/master/prices_30m.parquet
     is in fact a live, actively-refreshed master. The correction is
     prefixed onto the original sentence (never silently rewritten — the
     history event records what the operator believed on 2026-04-30).
  2. Revives both strategies ARCHIVED -> CANDIDATE through
     LifecycleStateMachine.transition() (guarded by
     VALID_TRANSITIONS[(ARCHIVED, CANDIDATE)]), naming the REAL blocker:
     both strategies read `market_data['*_30m_bars']`, a kwarg the backtest
     never passes (they never read `aux_data['prices_30m']`), so a
     candidate slot today would buy a 0-trade run. That fact is recorded
     both in the transition reason/history metadata AND in a top-level
     `backtest_quarantine` flag — confirmed read by
     scripts/refresh_backtests_resumable.js's `allStrategies()`
     (filters out `e.backtest_quarantine` entries from the nightly work
     queue) — so the revived candidates do NOT consume a nightly fleet
     slot while the wiring gap stands. The same fact is mirrored into the
     strategy's `metadata` (which round-trips through
     LifecycleStateMachine.to_dict(), unlike an ad-hoc top-level key — see
     to_dict()'s fixed key set) so it survives a later unrelated
     save_manifest() even if `backtest_quarantine` itself does not.

     KNOWN FRAGILITY (confirmed, not hypothetical): `backtest_quarantine`
     WILL be silently dropped the next time
     `strategies.lifecycle.auto_demote_negative_sharpe()` demotes ANY
     live/monitoring strategy elsewhere in the fleet — it loads the whole
     manifest via `from_manifest`, which does not carry unknown top-level
     keys into StrategyRecord, then calls `save_manifest()` -> `to_dict()`,
     whose fixed key set omits `backtest_quarantine` for every entry it
     touches (i.e. all of them, since `to_dict()` re-emits every loaded
     record). That auto-demote path runs from the daily
     execution.strategy_weights rebuild and is plausible within days.
     `metadata.revival_2026_09_13` DOES survive that (metadata is part of
     the schema), so a wiped `backtest_quarantine` is detectable and this
     script is safe to re-run to restore just that flag — see the
     'requarantine' phase below. This is the Task-9-OWED `to_dict()` gap;
     this script works around it rather than fixing it (fixing it means
     changing `to_dict()`'s fixed key set for every manifest writer, which
     is out of this task's scope).

Usage:
    python3 scripts/apply_qd_manifest_hygiene.py [--manifest PATH] [--dry-run]
    python3 scripts/apply_qd_manifest_hygiene.py [--manifest PATH] --apply

--dry-run is the DEFAULT: it reads the manifest, prints whether it is
byte-stable under write_atomic (see NON-ASCII NOTE below), computes the
change on an in-memory copy, and prints a unified diff. It writes nothing
and takes no lock. --apply performs the real read-modify-write, through
_manifest_lock.with_manifest_lock (the same cross-process lock JS writers —
saturday_brain.js, the finisher, approvals — use), preserving the file's
exact serialization convention (json.dumps(indent=2), no trailing newline,
via write_atomic). Before writing, --apply asserts (not just designs-for)
that no manifest entry OUTSIDE the two named strategies changed value —
belt-and-suspenders on top of the mutator only ever assigning
`strategies[sid]` for `sid` in the two targets.

NON-ASCII NOTE: `write_atomic`'s `json.dumps(payload, indent=2)` uses the
default `ensure_ascii=True`, which re-escapes any literal non-ASCII
character (seen in this manifest: '§', '→', '≥', probably others) into
`\\uXXXX` on every Python-side write — a pre-existing property of
_manifest_lock.py, not something this script introduces. If the manifest
contains such characters anywhere (as of 2026-09-17 it does, in ~180
places written by a JS caller, which does not escape them), a byte-level
diff of --apply's write will show churn far beyond the two target
strategies even though the PARSED value of every other entry is identical
(verified programmatically, not just by construction — see above). This
script's dry-run reports the byte-stability check so the operator sees
this before running --apply and is not surprised by the diff size.

Idempotent: a strategy already in the fully-applied state (candidate,
corrected reason, quarantined, revival metadata present) is left alone —
running --apply twice in a row is a no-op the second time. If the flag
fragility above has wiped `backtest_quarantine` since the last apply
(state/reason/metadata still correct, quarantine missing), a re-run
restores just that flag ('requarantine' phase) without re-transitioning
or re-touching history. The whole read-modify-write is a single lock
acquisition covering BOTH target strategies: if either is not in one of
its recognized phases (pending / requarantine / done), the script refuses
and names the offending strategy — and, because both edits share one
lock, nothing is written for either strategy, so a bad precondition on one
never leaves the other half-applied. It never changes the parsed value of
any manifest entry outside the two named strategies.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'

sys.path.insert(0, str(ROOT / 'src'))
try:
    from strategies.lifecycle import (
        LifecycleStateMachine, StrategyRecord, StrategyState, TransitionEvent,
    )
    from strategies import _manifest_lock as _ml
except ImportError:
    import importlib.util

    def _load(name: str, relpath: str):
        spec = importlib.util.spec_from_file_location(name, str(ROOT / relpath))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    _lifecycle = _load('strategies.lifecycle', 'src/strategies/lifecycle.py')
    LifecycleStateMachine = _lifecycle.LifecycleStateMachine
    StrategyRecord = _lifecycle.StrategyRecord
    StrategyState = _lifecycle.StrategyState
    TransitionEvent = _lifecycle.TransitionEvent
    _ml = _load('strategies._manifest_lock', 'src/strategies/_manifest_lock.py')


ACTOR = 'manual:operator'
# 2026-09-13 is the decision/spec date (when the A4 hygiene call was made,
# matching the corrected-reason text below and the spec date), NOT
# necessarily the date --apply is actually run. `state_since` (set by
# LifecycleStateMachine.transition() to the real transition timestamp) is
# the authoritative "when this actually happened" field; this constant is
# only ever used inside fixed prose/flag values, so it intentionally does
# not track the clock.
REVIVAL_DATE = '2026-09-13'
SPEC_REF = 'docs/specs/2026-09-12-quantdinger-adoptions-spec.md'

TARGETS = ('S_TR04_zarattini_intraday_spy', 'S_TR06_baltussen_eod_reversal')

OLD_REASON = (
    "Requires prices_30m which is no longer collected daily; "
    "shelved for future revival when intraday tier is enabled"
)
NEW_REASON = (
    "[CORRECTED 2026-09-13] Original reason (2026-04-30): 'Requires prices_30m "
    "which is no longer collected daily' — FALSE: data/master/prices_30m.parquet "
    "is a live master (refreshed 2026-09-11). The real blocker is different: this "
    "strategy reads intraday bars from the market_data kwarg, which the backtest "
    "never passes, and reads no aux_data['prices_30m']. That wiring is owed before "
    "any backtest of it can produce trades."
)
REVIVAL_REASON = (
    f"Revived {REVIVAL_DATE} (spec {SPEC_REF} A4 hygiene): the stated blocker — "
    "'prices_30m no longer collected' — is false; the master is live and refreshed "
    "2026-09-11. Queued as a candidate so the research lane can re-gate it. "
    "Backtest-quarantined until the intraday bars are wired: the strategy reads "
    "market_data['*_30m_bars'], a kwarg the backtest never passes, so a fleet slot "
    "today buys a 0-trade run and no information."
)
QUARANTINE_REASON = (
    "revived 2026-09-13 but reads market_data['*_30m_bars'], which the backtest "
    "never passes — 0-trade runs until the intraday aux wiring lands"
)


class PreconditionError(RuntimeError):
    """A target strategy is not in the expected pre-state or fully-applied
    post-state. Raised (and named) before any write happens."""


def _record_from_entry(sid: str, entry: dict) -> "StrategyRecord":
    history = [
        ev for ev in (TransitionEvent.from_row(e) for e in entry.get('history', []))
        if ev is not None
    ]
    return StrategyRecord(
        strategy_id=sid,
        state=StrategyState(entry['state']),
        state_since=entry['state_since'],
        history=history,
        metadata=entry.get('metadata', {}),
        eligible_regimes=entry.get('eligible_regimes'),
        universe_filter_ref=(entry.get('metadata', {}) or {}).get('universe_filter_ref'),
        instrument_class=entry.get('instrument_class', 'equity'),
    )


def _classify(entry: dict) -> str:
    """'pending' (fresh — ready to apply), 'requarantine' (already revived
    and reasoned, but `backtest_quarantine` is missing — see the module
    docstring's KNOWN FRAGILITY note: an unrelated auto_demote_negative_sharpe
    save_manifest() silently drops it), 'done' (fully applied — idempotent
    no-op), or 'unexpected' (anything else — caller refuses)."""
    state = entry.get('state')
    history = entry.get('history') or []
    has_old = any(ev.get('reason') == OLD_REASON for ev in history)
    has_new = any(ev.get('reason') == NEW_REASON for ev in history)
    quarantined = bool(entry.get('backtest_quarantine'))
    revival_meta = bool((entry.get('metadata') or {}).get('revival_2026_09_13'))

    if (state == StrategyState.ARCHIVED.value and has_old and not has_new
            and not quarantined and not revival_meta):
        return 'pending'
    if state == StrategyState.CANDIDATE.value and has_new and not has_old and revival_meta:
        return 'done' if quarantined else 'requarantine'
    return 'unexpected'


def _apply_one(sid: str, strategies: dict) -> str:
    """Mutate strategies[sid] in place for the pending/requarantine cases.
    Returns 'done' (no-op, already applied), 'requarantined' (flag
    restored, nothing else touched), or 'applied' (fresh revival). Raises
    PreconditionError naming `sid` for anything else — callers must not
    write on that path."""
    if sid not in strategies:
        raise PreconditionError(f"{sid}: not present in manifest")
    entry = strategies[sid]
    phase = _classify(entry)
    if phase == 'unexpected':
        history = entry.get('history') or []
        raise PreconditionError(
            f"{sid}: not in a recognized phase (pending / requarantine / done) — "
            f"refusing. "
            f"state={entry.get('state')!r} "
            f"has_old_reason={any(ev.get('reason') == OLD_REASON for ev in history)} "
            f"has_new_reason={any(ev.get('reason') == NEW_REASON for ev in history)} "
            f"backtest_quarantine={bool(entry.get('backtest_quarantine'))} "
            f"revival_metadata={bool((entry.get('metadata') or {}).get('revival_2026_09_13'))}. "
            f"A concurrent writer may have moved this strategy since the brief's facts "
            f"were verified — manual review needed, no changes written for either target."
        )
    if phase == 'done':
        return 'done'
    if phase == 'requarantine':
        # State/reason/metadata already correct — only the flag an
        # unrelated to_dict()-narrowing save_manifest() can drop is missing.
        entry['backtest_quarantine'] = {'reason': QUARANTINE_REASON, 'since': REVIVAL_DATE}
        return 'requarantined'

    # phase == 'pending' -> apply the reason correction + revival + quarantine.
    corrected = 0
    for ev in entry.get('history', []):
        if ev.get('reason') == OLD_REASON:
            ev['reason'] = NEW_REASON
            corrected += 1
    if corrected != 1:
        raise PreconditionError(
            f"{sid}: expected exactly 1 history event carrying the false reason, "
            f"found {corrected} — refusing, no changes written for either target."
        )

    revival_metadata = {
        'spec': SPEC_REF,
        'quarantine_reason': QUARANTINE_REASON,
        'owed': 'wire intraday 30m bars into aux_data',
    }
    rec = _record_from_entry(sid, entry)
    lsm = LifecycleStateMachine({sid: rec})
    lsm.transition(
        sid, StrategyState.CANDIDATE, actor=ACTOR, reason=REVIVAL_REASON,
        metadata={'revival_2026_09_13': revival_metadata},
    )
    new_entry = lsm.to_dict()['strategies'][sid]
    # LifecycleStateMachine.transition()'s `metadata` kwarg lands in the new
    # history event's `metadata` (already true via `metadata=` above), NOT
    # in the strategy's top-level `metadata` — only `universe_filter_ref` is
    # special-cased for that. Set it explicitly so it actually round-trips
    # through to_dict() as intended (see module docstring).
    new_entry['metadata']['revival_2026_09_13'] = revival_metadata
    new_entry['backtest_quarantine'] = {'reason': QUARANTINE_REASON, 'since': REVIVAL_DATE}
    strategies[sid] = new_entry
    return 'applied'


def _byte_stable(text: str) -> bool:
    """True iff re-serializing the parsed manifest through write_atomic's
    exact convention (json.dumps(indent=2), no trailing newline) reproduces
    `text` byte-for-byte. See the module docstring's NON-ASCII NOTE — this
    is commonly False on the real manifest for a known, benign reason
    (literal non-ASCII characters written by a JS caller, re-escaped by
    Python's default ensure_ascii=True), not necessarily a format drift."""
    return json.dumps(json.loads(text), indent=2) == text


def _semantic_diff_outside_targets(before: dict, after: dict) -> list[str]:
    """Return a list of things that changed VALUE (not byte-serialization)
    outside the two target strategies — empty means safe. Belt-and-suspenders
    check: `_apply_one` is only ever supposed to assign `strategies[sid]`
    for `sid` in TARGETS, so this should always be empty; a non-empty result
    means a bug and must block the write."""
    changed = []
    before_strats = (before or {}).get('strategies', {}) or {}
    after_strats = (after or {}).get('strategies', {}) or {}
    if set(before_strats) != set(after_strats):
        changed.append('<strategy set changed>')
    for sid, before_entry in before_strats.items():
        if sid in TARGETS:
            continue
        if after_strats.get(sid) != before_entry:
            changed.append(sid)
    for key in ('decommissioned', 'schema_version'):
        if (before or {}).get(key) != (after or {}).get(key):
            changed.append(f'<top-level key changed: {key}>')
    return changed


def run(manifest_path: Path, apply: bool) -> int:
    if not manifest_path.is_file():
        print(f"[apply_qd_manifest_hygiene] no manifest at {manifest_path}", file=sys.stderr)
        return 1

    original_text = manifest_path.read_text(encoding='utf-8')
    original = json.loads(original_text)
    byte_stable = _byte_stable(original_text)
    print(f"[apply_qd_manifest_hygiene] byte-stable under write_atomic: {byte_stable}"
          + ('' if byte_stable else
             " (expected if the manifest has literal non-ASCII characters written by a "
             "JS caller — see NON-ASCII NOTE in the script docstring; a real --apply "
             "diff will be larger than the two target strategies for that reason alone)"))

    if not apply:
        preview = copy.deepcopy(original)
        strategies = preview.setdefault('strategies', {})
        results: dict[str, str] = {}
        try:
            for sid in TARGETS:
                results[sid] = _apply_one(sid, strategies)
        except PreconditionError as exc:
            print(f"[apply_qd_manifest_hygiene] DRY-RUN — would REFUSE: {exc}", file=sys.stderr)
            return 1

        unsafe = _semantic_diff_outside_targets(original, preview)
        if unsafe:
            print(f"[apply_qd_manifest_hygiene] DRY-RUN — internal check failed, refusing "
                  f"to preview: entries changed outside {TARGETS}: {unsafe}", file=sys.stderr)
            return 1

        new_text = json.dumps(preview, indent=2)
        if new_text == original_text:
            print("[apply_qd_manifest_hygiene] DRY-RUN: manifest already fully hygiened "
                  "— --apply would be a no-op.")
            for sid, phase in results.items():
                print(f"  {sid}: {phase}")
            return 0

        print("[apply_qd_manifest_hygiene] DRY-RUN — no changes written. Planned:")
        for sid, phase in results.items():
            print(f"  {sid}: {phase}")
        print("[apply_qd_manifest_hygiene] confirmed: no manifest entry outside "
              f"{TARGETS} changes value (byte-level diff below may still show "
              "non-ASCII re-escaping churn elsewhere — see byte-stable line above).")
        print()
        sys.stdout.writelines(difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{manifest_path} (current)",
            tofile=f"{manifest_path} (after --apply)",
        ))
        return 0

    # --apply: one locked read-modify-write covering both targets.
    results: dict[str, str] = {}

    def _mutate(disk: dict) -> dict:
        before = copy.deepcopy(disk)
        strategies = disk.setdefault('strategies', {})
        for sid in TARGETS:
            results[sid] = _apply_one(sid, strategies)
        unsafe = _semantic_diff_outside_targets(before, disk)
        if unsafe:
            # Defensive only — _apply_one only ever assigns strategies[sid]
            # for sid in TARGETS, so this should be unreachable. Refuse
            # rather than write if it ever isn't.
            raise PreconditionError(
                f"internal check failed, refusing to write: entries changed "
                f"outside {TARGETS}: {unsafe}"
            )
        return disk

    try:
        _ml.with_manifest_lock(manifest_path, _mutate, actor='qd-stream-a:manifest-hygiene')
    except PreconditionError as exc:
        print(f"[apply_qd_manifest_hygiene] APPLY REFUSED: {exc}", file=sys.stderr)
        return 1

    print("[apply_qd_manifest_hygiene] APPLY complete.")
    verbs = {
        'done': 'no-op (already applied)',
        'requarantined': 'requarantined (flag had been wiped by an unrelated '
                          'save_manifest() — restored; nothing else touched)',
        'applied': 'applied',
    }
    for sid, phase in results.items():
        print(f"  {sid}: {verbs.get(phase, phase)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stream A Task 6 manifest hygiene: correct the two false 'prices_30m "
            "no longer collected' reasons and revive S_TR04_zarattini_intraday_spy / "
            "S_TR06_baltussen_eod_reversal ARCHIVED -> CANDIDATE, quarantined."
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
