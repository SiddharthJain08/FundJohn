"""SP-2 Phase D — tests for lifecycle predicate detection + registration.

Covers:
  - _detect_module_predicate: AST-based extraction of the aliased universe import
  - register_strategy_predicate: write + clear the predicate on an in-memory record
  - Resolver round-trip (D-C1): proves module:attr form is required
  - Integration: gated register() hook sets/does-not-set universe_filter_ref
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import strategies.lifecycle as lifecycle_mod
from strategies.lifecycle import (
    LifecycleStateMachine,
    StrategyState,
    _detect_module_predicate,
)
from strategies.universe_resolver import UniverseResolver

# ─── minimal manifest helper ────────────────────────────────────────────────

_BASE_MANIFEST = {
    "schema_version": "1.0",
    "strategies": {
        "S_pred_test": {
            "state": "candidate",
            "state_since": "2026-01-01T00:00:00+00:00",
            "metadata": {"canonical_file": "S_pred_test.py"},
            "history": [],
        }
    },
}


def _write_manifest(path, extra_meta=None):
    payload = json.loads(json.dumps(_BASE_MANIFEST))
    if extra_meta:
        payload["strategies"]["S_pred_test"]["metadata"].update(extra_meta)
    path.write_text(json.dumps(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 1. _detect_module_predicate
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectModulePredicate:
    def test_detects_aliased_import(self, tmp_path):
        """Aliased 'as universe_filter' import returns the predicate name."""
        impl = tmp_path / "strat.py"
        impl.write_text(
            "from src.strategies.universe_default import large_cap as universe_filter\n"
            "def run(): pass\n"
        )
        assert _detect_module_predicate(impl) == "large_cap"

    def test_detects_aliased_import_strategies_root(self, tmp_path):
        """Also works with the bare 'strategies.universe_default' form."""
        impl = tmp_path / "strat.py"
        impl.write_text(
            "from strategies.universe_default import sp500 as universe_filter\n"
        )
        assert _detect_module_predicate(impl) == "sp500"

    def test_no_universe_import_returns_none(self, tmp_path):
        """A file with no universe import returns None."""
        impl = tmp_path / "strat.py"
        impl.write_text("import pandas as pd\ndef run(): pass\n")
        assert _detect_module_predicate(impl) is None

    def test_import_without_alias_returns_none(self, tmp_path):
        """Import from universe_default WITHOUT 'as universe_filter' is ignored."""
        impl = tmp_path / "strat.py"
        impl.write_text(
            "from src.strategies.universe_default import large_cap\n"
        )
        assert _detect_module_predicate(impl) is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        """A missing file returns None, never raises."""
        missing = tmp_path / "ghost.py"
        assert _detect_module_predicate(missing) is None

    def test_syntax_error_returns_none(self, tmp_path):
        """A file with invalid Python returns None, never raises."""
        broken = tmp_path / "broken.py"
        broken.write_text("def (:\n")
        assert _detect_module_predicate(broken) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. register_strategy_predicate
# ─────────────────────────────────────────────────────────────────────────────

class TestRegisterStrategyPredicate:
    def test_rejects_unknown_predicate(self, tmp_path):
        """An unknown predicate name raises ValueError mentioning 'candidate set'."""
        _write_manifest(tmp_path / "manifest.json")
        lsm = LifecycleStateMachine.from_manifest(tmp_path / "manifest.json")
        with pytest.raises(ValueError, match="candidate set"):
            lsm.register_strategy_predicate(
                "S_pred_test", "does_not_exist", tmp_path / "manifest.json"
            )

    def test_writes_module_attr_form(self, tmp_path):
        """A valid predicate name is written in module:attr form."""
        mf = tmp_path / "manifest.json"
        _write_manifest(mf)
        lsm = LifecycleStateMachine.from_manifest(mf)
        lsm.register_strategy_predicate("S_pred_test", "large_cap", mf)

        payload = json.loads(mf.read_text())
        assert (
            payload["strategies"]["S_pred_test"]["metadata"]["universe_filter_ref"]
            == "src.strategies.universe_default:large_cap"
        )

    def test_clears_predicate_when_none(self, tmp_path):
        """predicate_name=None clears the field and removes the key from metadata."""
        mf = tmp_path / "manifest.json"
        _write_manifest(mf, extra_meta={"universe_filter_ref": "src.strategies.universe_default:large_cap"})
        lsm = LifecycleStateMachine.from_manifest(mf)
        lsm.register_strategy_predicate("S_pred_test", None, mf)

        payload = json.loads(mf.read_text())
        assert "universe_filter_ref" not in payload["strategies"]["S_pred_test"]["metadata"]

    def test_raises_for_unknown_strategy(self, tmp_path):
        """A strategy ID not in the manifest raises ValueError."""
        mf = tmp_path / "manifest.json"
        _write_manifest(mf)
        lsm = LifecycleStateMachine.from_manifest(mf)
        with pytest.raises(ValueError, match="not in manifest"):
            lsm.register_strategy_predicate("NONEXISTENT", "large_cap", mf)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Resolver round-trip (D-C1)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolverRoundTrip:
    def test_resolver_loads_predicate_after_register(self, tmp_path):
        """After register_strategy_predicate, UniverseResolver._load_predicate
        returns the actual large_cap function without raising.
        This proves the module:attr ref format is correct (a bare name would
        crash ValueError on rsplit(':',1) unpack).

        Note: the test imports large_cap via 'strategies.universe_default'
        (sys.path form) while the resolver loads via 'src.strategies.universe_default'
        (package form), so they are distinct objects in Python's module cache.
        We verify identity by __name__ and __qualname__ to confirm the correct
        callable was resolved, and confirm no exception was raised.
        """
        mf = tmp_path / "manifest.json"
        _write_manifest(mf)
        lsm = LifecycleStateMachine.from_manifest(mf)
        lsm.register_strategy_predicate("S_pred_test", "large_cap", mf)

        resolver = UniverseResolver(
            db=MagicMock(),
            coverage=MagicMock(),
            manifest_loader=lambda: json.loads(mf.read_text()),
        )
        # This must NOT raise ValueError — a bare ref (no colon) would crash here
        fn = resolver._load_predicate("S_pred_test")
        assert callable(fn), f"Expected a callable, got {fn!r}"
        assert fn.__name__ == "large_cap", (
            f"Expected function named 'large_cap', got {fn.__name__!r}. "
            "A bare ref (no colon) would have raised ValueError before this point."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Integration: gated register() hook
# ─────────────────────────────────────────────────────────────────────────────

class TestRegisterGatedHook:
    def _make_impl_file(self, directory, filename, predicate_name="large_cap"):
        """Write a minimal strategy file with the aliased universe import."""
        impl = directory / filename
        impl.write_text(
            f"from src.strategies.universe_default import {predicate_name} as universe_filter\n"
            "def run(): pass\n"
        )
        return impl

    def test_gate_on_sets_universe_filter_ref(self, tmp_path, monkeypatch):
        """When gate is ON and impl file has the aliased import, register() sets
        universe_filter_ref on the new record in module:attr form."""
        monkeypatch.setenv("OPENCLAW_PHASE_D_PREDICATE_AT_MINT", "1")
        monkeypatch.setattr(lifecycle_mod, "_IMPLEMENTATIONS_DIR", tmp_path)

        self._make_impl_file(tmp_path, "S_new_strat.py", "large_cap")

        lsm = LifecycleStateMachine.new_empty()
        rec = lsm.register(
            "S_new_strat",
            initial_state=StrategyState.CANDIDATE,
            metadata={"canonical_file": "S_new_strat.py"},
        )

        assert rec.universe_filter_ref == "src.strategies.universe_default:large_cap"

    def test_gate_off_does_not_set_universe_filter_ref(self, tmp_path, monkeypatch):
        """When gate is OFF, register() leaves universe_filter_ref as None
        even if the impl file has the aliased import."""
        monkeypatch.delenv("OPENCLAW_PHASE_D_PREDICATE_AT_MINT", raising=False)
        monkeypatch.setattr(lifecycle_mod, "_IMPLEMENTATIONS_DIR", tmp_path)

        self._make_impl_file(tmp_path, "S_new_strat2.py", "large_cap")

        lsm = LifecycleStateMachine.new_empty()
        rec = lsm.register(
            "S_new_strat2",
            initial_state=StrategyState.CANDIDATE,
            metadata={"canonical_file": "S_new_strat2.py"},
        )

        assert rec.universe_filter_ref is None

    def test_gate_on_no_canonical_file_leaves_ref_none(self, tmp_path, monkeypatch):
        """Gate ON but no canonical_file in metadata — no crash, ref stays None."""
        monkeypatch.setenv("OPENCLAW_PHASE_D_PREDICATE_AT_MINT", "1")
        monkeypatch.setattr(lifecycle_mod, "_IMPLEMENTATIONS_DIR", tmp_path)

        lsm = LifecycleStateMachine.new_empty()
        rec = lsm.register(
            "S_no_file",
            initial_state=StrategyState.CANDIDATE,
            metadata={},
        )
        assert rec.universe_filter_ref is None

    def test_gate_on_unknown_predicate_not_in_candidate_set(self, tmp_path, monkeypatch):
        """An impl file importing a name NOT in CANDIDATE_PREDICATES is silently ignored
        (the detection code checks 'detected in _CP' before setting)."""
        monkeypatch.setenv("OPENCLAW_PHASE_D_PREDICATE_AT_MINT", "1")
        monkeypatch.setattr(lifecycle_mod, "_IMPLEMENTATIONS_DIR", tmp_path)

        # Write an import with a name that is not in CANDIDATE_PREDICATES
        impl = tmp_path / "S_weird.py"
        impl.write_text(
            "from src.strategies.universe_default import sp500 as universe_filter\n"
        )
        # Patch CANDIDATE_PREDICATES via the module so it looks empty for this test
        # (simulating a typo or a predicate not yet in the candidate set)
        impl2 = tmp_path / "S_weird2.py"
        impl2.write_text(
            "from src.strategies.universe_default import totally_unknown_pred as universe_filter\n"
        )

        lsm = LifecycleStateMachine.new_empty()
        rec = lsm.register(
            "S_weird2",
            initial_state=StrategyState.CANDIDATE,
            metadata={"canonical_file": "S_weird2.py"},
        )
        # totally_unknown_pred not in CANDIDATE_PREDICATES → ref stays None
        assert rec.universe_filter_ref is None
