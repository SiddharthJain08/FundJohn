"""tests/test_vertical_close.py — SP-5.2 Task 4: debit vertical closes via per-leg position close.

Verifies that a vertical close_only order:
  - routes through _route_mleg_close (structure-agnostic)
  - closes each held leg via `position close --symbol-or-asset-id <occ>` (per-leg)
  - NEVER submits a net-credit mleg package (no --order-class, no legs JSON)
"""
import sys
sys.path.insert(0, 'src')
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


# OCC symbols representing a held debit call vertical:
#   long near leg (lower strike)  +N
#   short far leg (higher strike) -N
_NEAR_OCC = 'SPY260718C00500000'
_FAR_OCC  = 'SPY260718C00515000'


def test_vertical_close_flattens_both_legs(monkeypatch):
    """Both held legs are closed via per-leg `position close`; no mleg order submitted."""
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call',
                      strike_rule='atm', spread_width_pct=0.03)
    order = {
        'ticker': 'SPY',
        'instrument_class': 'option',
        'direction': 'long',
        'option_spec': spec,
        'close_only': True,
    }
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    # _held_option_legs returns plain OCC strings (structure-agnostic)
    monkeypatch.setattr(ex, '_held_option_legs',
                        lambda und: [_NEAR_OCC, _FAR_OCC])
    calls = []
    monkeypatch.setattr(ex, '_run_alpaca_cli',
                        lambda args: (calls.append(args) or (True, {'status': 'accepted'}, None)))

    res = ex._route_option_order(order, equity=100_000.0, coid='vc1')

    # --- result shape ---
    assert res is not None
    assert res.get('status') == 'submitted'
    assert res.get('structure') == 'vertical'
    assert set(res.get('legs', [])) == {_NEAR_OCC, _FAR_OCC}

    # --- per-leg close: exactly 2 CLI calls, both are `position close` ---
    assert len(calls) == 2, f"expected 2 per-leg closes, got {len(calls)}: {calls}"
    for c in calls:
        assert c[:2] == ['position', 'close'], (
            f"expected 'position close' call, got {c}")

    # --- NO net-credit mleg package ---
    # A mleg sell-package would contain '--order-class' or legs JSON.
    # Confirm neither appears in any call.
    all_args = [arg for c in calls for arg in c]
    assert '--order-class' not in all_args, (
        "net-credit mleg submit detected: '--order-class' in CLI args")
    assert 'mleg' not in all_args, (
        "net-credit mleg submit detected: 'mleg' in CLI args")

    # --- both OCC symbols were individually closed ---
    closed_syms = [c[c.index('--symbol-or-asset-id') + 1]
                   for c in calls if '--symbol-or-asset-id' in c]
    assert set(closed_syms) == {_NEAR_OCC, _FAR_OCC}, (
        f"expected both legs closed individually, got {closed_syms}")


def test_vertical_close_no_held_legs_returns_skip(monkeypatch):
    """If no option positions are held, the close returns a skip (not an error)."""
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call',
                      strike_rule='atm', spread_width_pct=0.03)
    order = {
        'ticker': 'SPY',
        'instrument_class': 'option',
        'direction': 'long',
        'option_spec': spec,
        'close_only': True,
    }
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_held_option_legs', lambda und: [])

    res = ex._route_option_order(order, equity=100_000.0, coid='vc2')
    assert res is not None
    assert res.get('status') == 'skipped'
    assert 'no held legs' in (res.get('reason') or '')


def test_vertical_close_gate_off_skips_fail_closed(monkeypatch):
    """NIT-1 contract: an option CLOSE with the gate OFF skips fail-closed —
    it must NEVER fall through to an equity position-close on the underlying."""
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call')
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': spec, 'close_only': True}
    res = ex._route_option_order(order, equity=100_000.0, coid='vc3')
    assert res is not None and res.get('status') == 'skipped'
    assert 'gate is OFF' in (res.get('reason') or '')
