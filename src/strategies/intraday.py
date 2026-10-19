"""strategies.intraday — authoring helper for single-session (intraday) strategies.

Pure, no I/O. A strategy opts into the intraday session simulator by putting
BOTH `exit_on='session_close'` and `entry_bar_et='HH:MM'` in its
`Signal.signal_params`; build them with `session_close_params` so a bad bar
string fails at authoring time instead of silently skipping trades.

Contract: docs/specs/2026-10-19-intraday-session-simulator-spec.md
  - section 2: the opt-in keys and the entry-bar-after-last-read rule
  - section 3: simulator semantics (fill = entry bar open, flat at last RTH bar)
  - section 6: live execution is NOT supported; the promotion gate refuses a run
    with intraday_trades > 0 unless OPENCLAW_INTRADAY_EXIT_LIVE=1.
The lint (`strategy_lint`, kind `intraday`) enforces the same bar set statically.
"""
from __future__ import annotations

EXIT_ON_SESSION_CLOSE = 'session_close'

# Start times (ET) of the 13 regular-hours 30-minute bars: 09:30 .. 15:30.
RTH_BAR_STARTS_ET = tuple(
    f'{(570 + 30 * i) // 60:02d}:{(570 + 30 * i) % 60:02d}' for i in range(13)
)


def session_close_params(entry_bar_et: str, /, **extra) -> dict:
    """Return signal_params for a session_close signal.

    `entry_bar_et` must be one of RTH_BAR_STARTS_ET. `extra` keys are merged
    in but may not override `exit_on` or `entry_bar_et`.
    """
    if entry_bar_et not in RTH_BAR_STARTS_ET:
        raise ValueError(
            f'entry_bar_et {entry_bar_et!r} is not an RTH 30-minute bar start; '
            f'allowed: {RTH_BAR_STARTS_ET}')
    clash = sorted({'exit_on', 'entry_bar_et'} & set(extra))
    if clash:
        raise ValueError(f'session_close_params: extra may not override {clash}')
    return {**extra, 'exit_on': EXIT_ON_SESSION_CLOSE, 'entry_bar_et': entry_bar_et}


def is_session_close(signal_params) -> bool:
    """True when signal_params opts into the session simulator (None-safe)."""
    return isinstance(signal_params, dict) and signal_params.get('exit_on') == EXIT_ON_SESSION_CLOSE
