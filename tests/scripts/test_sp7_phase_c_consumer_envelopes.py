# tests/test_sp7_phase_c_consumer_envelopes.py
"""SP-7 Phase C Task 15 — envelope assertions for no-change consumers (spec §5)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_redeploy_inherits_engine_universe():
    """Redeploy re-runs the engine signals step — it must have NO independent
    universe authority (it inherits C1 automatically)."""
    src = (ROOT / "scripts/redeploy_pipeline.py").read_text()
    assert "REDEPLOY_STEPS = 'signals,handoff,trade,alpaca,reconcile'" in src
    assert "universe_resolver" not in src
    assert "universe_config" not in src


def test_screener_inserts_inactive_only():
    """Screener candidates land active=false — the operator overlay stays the
    sole promotion path, and active=false remains a hard exclusion (C2)."""
    src = (ROOT / "src/pipeline/alpaca_screener.js").read_text()
    insert = re.search(r"INSERT INTO universe_config[\s\S]{0,400}", src)
    assert insert, "screener no longer inserts into universe_config?"
    assert re.search(r"\bfalse\b", insert.group(0)), \
        "screener INSERT no longer pins active=false"


def test_sentiment_default_path_is_three_source_union():
    """Gate off -> current_universe (universe_config u positions u 7d signals)."""
    src = (ROOT / "src/pipeline/run_sentiment_step.py").read_text()
    assert "current_universe(pg_uri)" in src
    assert "_widen_with_resolver" in src     # Task 13 wiring present, gated


def test_options_archive_default_path_is_universe_config():
    src = (ROOT / "src/pipeline/backfillers/alpaca_options.py").read_text()
    assert "_resolver_archive_universe(date) or _load_universe()" in src


def test_daily_cycle_graph_does_not_wire_loadPerStrategyUniverse():
    """SP-2-era helper is dead code superseded by C1's in-engine resolution
    (engine.py OPENCLAW_LIVE_UNIVERSE_RESOLVER). If this fails, someone wired
    it -- make sure that's deliberate and doesn't double-resolve per cycle.

    Verified count 2026-06-07: 3 mentions total --
      line 73: function definition
      line 86: console.warn inside the function body
      line 263: module.exports entry
    Any MORE means a call site appeared.
    """
    src = (ROOT / "src/agent/graphs/daily-cycle.js").read_text()
    defs = src.count("loadPerStrategyUniverse")
    # definition + console.warn inside it + module.exports = 3 mentions; any
    # MORE means a call site appeared.
    assert defs == 3, f"loadPerStrategyUniverse mention count changed: {defs}"
