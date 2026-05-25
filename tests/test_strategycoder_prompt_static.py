from pathlib import Path


def test_strategycoder_has_phase_d_section():
    p = Path('src/agent/prompts/subagents/strategycoder.md').read_text()
    assert 'Universe predicate' in p
    assert 'INFERRED_UNIVERSE_FILTER' in p
    assert 'universe_default import' in p   # the import line
    assert 'as universe_filter' in p
    assert 'module scope' in p.lower() or 'module-scope' in p.lower()
    # null-case prohibition must be present
    assert 'Do NOT define' in p
    # section sits within Artifact 1, before the Data dependency section
    assert p.index('### Universe predicate') < p.index('### Data dependency declaration')
