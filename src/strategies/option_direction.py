"""SP-5.1c — normalize a strategy's emitted direction to long/short for the
option order-build path. Mirrors the server.js normalizer (LONG/BUY/BUY_VOL->long,
SHORT/SELL/SELL_VOL->short). Vol-direction (BUY_VOL/SELL_VOL) is what option
strategies emit (e.g. shv15_iv_term_structure, S_short_straddle_vrp)."""

_LONG = {'long', 'buy', 'buy_vol'}
_SHORT = {'short', 'sell', 'sell_vol'}


def normalize_option_direction(direction):
    """Return 'long' | 'short' | None (fail-closed on unknown/empty)."""
    if not direction:
        return None
    u = str(direction).strip().lower()
    if u in _LONG:
        return 'long'
    if u in _SHORT:
        return 'short'
    return None
