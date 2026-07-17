import re
from pathlib import Path


def test_paperhunter_has_phase_d_section():
    p = Path('src/agent/prompts/subagents/paperhunter.md').read_text()
    assert '## Step 5 — Infer universe slice' in p
    assert 'inferred_universe_filter' in p

    # regression guard: all 12 vetted predicate names must be present
    for name in [
        'sp500', 'r1000', 'r3000', 'options_eligible_only', 'large_cap',
        'mid_cap', 'small_cap_liquid', 'large_cap_options', 'mid_cap_options',
        'no_adr', 'no_otc', 'top500_by_adv',
    ]:
        assert name in p, f"Missing predicate name: {name}"

    # ensure no phantom names leaked from the spec draft
    for bogus in [
        'tech_sector', 'etfs_only', 'high_vol_20d', 'value_quintile',
        'momentum_quintile', 'low_corr', 'defensive',
    ]:
        assert bogus not in p, f"Phantom predicate name present: {bogus}"

    # guard against bare `options_eligible"` (spec-draft artifact) without
    # false-positiving on the real `options_eligible_only`
    assert 'options_eligible"' not in p
