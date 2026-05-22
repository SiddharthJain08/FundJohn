from pathlib import Path
import pytest
from src.strategies.universe_lint import scan_module, LintError

FIX = Path(__file__).parent / "fixtures" / "universe_lint"

def test_good_predicate_passes():
    errors = scan_module(FIX / "good_predicate.py")
    assert errors == []

def test_bad_signature_fails():
    errors = scan_module(FIX / "bad_signature.py")
    assert any("signature" in e.message for e in errors)

def test_bad_import_fails():
    errors = scan_module(FIX / "bad_import.py")
    assert any("forbidden import" in e.message for e in errors)

def test_transitive_today_fails():
    errors = scan_module(FIX / "transitive_today.py")
    assert any("transitive" in e.message for e in errors)

def test_scan_module_returns_pathed_errors():
    errors = scan_module(FIX / "bad_signature.py")
    assert all(str(FIX) in str(e.path) for e in errors)
