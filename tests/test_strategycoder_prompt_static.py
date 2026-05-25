from pathlib import Path


def test_strategycoder_has_phase_d_section():
    p = Path('src/agent/prompts/subagents/strategycoder.md').read_text()
    assert 'Universe predicate' in p
    assert 'INFERRED_UNIVERSE_FILTER' in p
    assert 'universe_default import' in p   # the import line
    assert 'as universe_filter' in p
    assert 'module scope' in p.lower() or 'module-scope' in p.lower()
