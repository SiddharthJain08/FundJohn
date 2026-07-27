"""Pre-trade account-state assertion (fix 6, 2026-07-27): the 2026-07-23
incident class — multiplier 4→1 + shorting revoked — must halt new sizing
loudly instead of silently planning an unholdable book."""
import importlib

live = importlib.import_module("execution.regime_blended_sizer_live")

HEALTHY = {
    'multiplier': 4.0, 'shorting_enabled': True, 'trading_blocked': False,
    'account_blocked': False, 'status': 'ACTIVE', 'fetched': True,
}


def _v(account, params=None):
    return live._account_state_violations(account, params=params or {})


def test_healthy_account_passes():
    assert _v(dict(HEALTHY)) == []


def test_multiplier_flip_caught():
    acct = dict(HEALTHY, multiplier=1.0)
    out = _v(acct)
    assert len(out) == 1 and 'multiplier' in out[0]


def test_live_regt_multiplier_2_passes():
    assert _v(dict(HEALTHY, multiplier=2.0)) == []


def test_shorting_revoked_caught():
    out = _v(dict(HEALTHY, shorting_enabled=False))
    assert any('shorting' in s for s in out)


def test_shorting_check_can_be_waived():
    out = _v(dict(HEALTHY, shorting_enabled=False),
             params={'require_shorting_enabled': '0'})
    assert out == []


def test_blocked_flags_caught():
    out = _v(dict(HEALTHY, trading_blocked=True, account_blocked=True))
    assert len(out) == 2


def test_non_active_status_caught():
    out = _v(dict(HEALTHY, status='RESTRICTED'))
    assert any('RESTRICTED' in s for s in out)


def test_missing_optional_fields_pass():
    # Old-shape account dict (no sanity fields) must not false-positive —
    # fail-open when the API/fetch doesn't report them.
    assert _v({'equity': 100000.0, 'multiplier': 4.0, 'fetched': True}) == []


def test_min_multiplier_configurable():
    out = _v(dict(HEALTHY, multiplier=2.0), params={'min_account_multiplier': '4'})
    assert len(out) == 1 and 'multiplier' in out[0]
