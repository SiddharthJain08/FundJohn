"""Canonical resolver for the signal target-date mode (§8, 2026-08-06 spec).

The mode selector used to be OPENCLAW_EOD_SIGNAL_REGISTER, whose LIVE value
is 0 — i.e. the production same-day configuration was expressed as a flag
being OFF, inverting the fleet-wide convention that 1 means "the live
thing". Operator directive: make 1 uniformly mean live.

New positive flag: OPENCLAW_SAMEDAY_SIGNAL_TARGET
    1 → signals target the SAME day (target_date=T; live since the
        2026-07-29 same-day pivot)
    0 → legacy T+1 EOD-register semantics (target_date=next trading day)

Migration-with-alias (operator-approved 2026-08-06): when the new flag is
set it wins; when unset, the legacy flag is honoured exactly as before
(register=1 ⇒ T+1, register=0/unset ⇒ same-day). After one epoch the
legacy name gets retired. doctor's env interlock FAILs when both are set
and contradict, so a half-migrated environment is loud instead of silently
picking a winner.

Model: OPENCLAW_SAMEDAY_EXEC=1, which was already correctly polarised.
"""
from __future__ import annotations

import os

NEW_FLAG = 'OPENCLAW_SAMEDAY_SIGNAL_TARGET'
LEGACY_FLAG = 'OPENCLAW_EOD_SIGNAL_REGISTER'


def sameday_signal_target_on() -> bool:
    """True ⇒ same-day target_date=T (the live configuration)."""
    new = os.environ.get(NEW_FLAG)
    if new is not None and new != '':
        return new == '1'
    return os.environ.get(LEGACY_FLAG) != '1'


def eod_register_on() -> bool:
    """Inverse view for legacy call sites: True ⇒ T+1 EOD-register flow."""
    return not sameday_signal_target_on()


def legacy_alias_conflict() -> str | None:
    """Non-None (a description) when BOTH flags are set and they contradict.

    The alias rule above resolves the contradiction (new flag wins), but a
    contradictory environment means someone edited one flag and not the
    other — exactly the half-migration mistake the doctor should FAIL on
    rather than let the resolver silently pick a side.
    """
    new = os.environ.get(NEW_FLAG)
    legacy = os.environ.get(LEGACY_FLAG)
    if new in (None, '') or legacy in (None, ''):
        return None
    if (new == '1') == (legacy == '1'):
        return (f'{NEW_FLAG}={new} contradicts {LEGACY_FLAG}={legacy}: '
                f'same-day target and T+1 EOD register are mutually '
                f'exclusive — the resolver honours the NEW flag, but fix '
                f'the environment')
    return None
