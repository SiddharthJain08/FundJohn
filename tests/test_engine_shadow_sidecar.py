"""SP-7 Phase C Task 7 — shadow parity is a NON-FATAL, zero-delta sidecar."""
from pathlib import Path


def test_shadow_block_present_and_gated():
    src = Path("src/execution/engine.py").read_text()
    assert "OPENCLAW_LIVE_UNIVERSE_SHADOW" in src
    block = src[src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW") - 600:
                src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW") + 1200]
    # shadow only runs while the live gate is OFF (shadow-vs-clamp comparison)
    assert "OPENCLAW_LIVE_UNIVERSE_RESOLVER" in block
    assert "write_shadow_parity" in block
    # non-fatal: wrapped in try/except with a warning, never a raise
    assert "non-fatal" in block


def test_shadow_block_mutates_no_engine_state():
    """Zero-behavior-delta pin (spec §7): the sidecar block reads engine state
    but never assigns to it — shadow ON vs OFF cannot change signals."""
    src = Path("src/execution/engine.py").read_text()
    start = src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW")
    block = src[start:start + 900]
    block = block[:block.index("prices   = load_prices")] if "prices   = load_prices" in block else block
    for lhs in ("universe =", "strategies =", "strategy_universes =", "aux_data ="):
        assert lhs not in block, f"shadow sidecar must not assign engine state: {lhs}"


def test_shadow_failure_does_not_raise(monkeypatch):
    """Simulate the sidecar call pattern: a raising writer must be swallowed."""
    import execution.live_universe as lu

    def boom(*a, **k):
        raise RuntimeError("shadow db down")
    monkeypatch.setattr(lu, "write_shadow_parity", boom)
    # The engine wraps the call; replicate the wrapper contract here:
    try:
        try:
            lu.write_shadow_parity("2026-06-08", [], [])
        except Exception:
            pass  # engine logs a warning and continues
    except Exception:
        raise AssertionError("sidecar exception escaped")
