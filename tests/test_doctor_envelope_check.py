"""SP-7 Phase C Task 12 — doctor envelope freshness check."""


def test_gate_off_is_ok(monkeypatch):
    monkeypatch.delenv("OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE", raising=False)
    from src.maintenance.doctor import PASS, check_collector_envelope_freshness
    out = check_collector_envelope_freshness()
    assert out["severity"] == PASS   # doctor severities are lowercase strings ('pass')
    assert "gate off" in out["detail"]


def test_check_is_registered_slow():
    from src.maintenance.doctor import check_collector_envelope_freshness as fn
    assert fn._check_name == "collector_envelope_freshness"
    assert fn._slow is True
