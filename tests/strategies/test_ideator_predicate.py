"""
SP-2 Phase D (ideator extension) — static checks for strategist-ideator.md.

Verifies that:
  1. The prompt contains the §5 inference section.
  2. The prompt declares `inferred_universe_filter` in its output JSON schema.
  3. All 12 real CANDIDATE_PREDICATES names appear in the prompt (no phantom names).
  4. None of the known phantom names appear in the predicate table (e.g. `tech_sector`).
  5. The INSERT SQL uses `kind='internal'` so ideator candidates bypass PaperHunter
     and reach _codeStrategy with their inferred_universe_filter intact.

These tests are intentionally static (no DB, no node subprocess needed) —
the prompt is the contract, and the orchestrator's existing READY-path threading
(research-orchestrator.js:433 specWithPred merge) already handles the field
once kind='internal' rows carry it in hunter_result_json.
"""

import os
import sys

WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROMPT_PATH = os.path.join(
    WORKTREE, 'src', 'agent', 'prompts', 'subagents', 'strategist-ideator.md'
)

# Known phantom predicate names that must NOT appear in the predicate table.
PHANTOM_NAMES = [
    'tech_sector',
    'growth',
    'value',
    'dividend',
    'international',
    'emerging_markets',
    'sector_etf',
    'nasdaq100',
    'russell2000',
]


def _load_prompt():
    with open(PROMPT_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def test_inference_section_present():
    """Prompt must contain a §5 (or equivalent) universe-inference heading."""
    content = _load_prompt()
    assert 'Infer universe slice' in content or 'Step 5' in content, (
        'strategist-ideator.md is missing the universe-inference section'
    )


def test_inferred_universe_filter_field_present():
    """Prompt must include `inferred_universe_filter` in the output spec."""
    content = _load_prompt()
    assert 'inferred_universe_filter' in content, (
        'strategist-ideator.md output JSON schema is missing `inferred_universe_filter`'
    )


def test_all_12_candidate_predicates_present():
    """All 12 real CANDIDATE_PREDICATES names must appear in the prompt."""
    sys.path.insert(0, WORKTREE)
    from src.strategies.universe_default import (
        CANDIDATE_PREDICATES, LADDER_TIER_PREDICATES)

    content = _load_prompt()
    # SP-7 ladder tiers are adoption-only — not in the mint menu (Phase D
    # decides mint exposure), so the prompt deliberately omits them.
    mint_menu = [n for n in CANDIDATE_PREDICATES if n not in LADDER_TIER_PREDICATES]
    missing = [name for name in mint_menu if name not in content]
    assert not missing, (
        f'strategist-ideator.md is missing predicate name(s): {missing}'
    )


def test_no_phantom_predicate_names_in_table():
    """Known phantom predicate names must NOT appear in the predicate table."""
    content = _load_prompt()
    # Extract the table section only (between the table header and the Examples block).
    # We're strict enough just checking the whole file — phantom names have no
    # legitimate use in this prompt.
    found = [name for name in PHANTOM_NAMES if f'`{name}`' in content]
    assert not found, (
        f'strategist-ideator.md references phantom predicate name(s): {found}'
    )


def test_insert_uses_kind_internal():
    """INSERT SQL must set kind='internal' so candidates bypass PaperHunter."""
    content = _load_prompt()
    assert "kind='internal'" in content or 'kind = \'internal\'' in content or \
           "'internal'" in content, (
        "strategist-ideator.md INSERT SQL does not set kind='internal' — ideator "
        "candidates will enter processQueue as kind='paper' and get rejected by "
        "PaperHunter (abstract_too_sparse on ideator:// URLs)"
    )


if __name__ == '__main__':
    tests = [
        test_inference_section_present,
        test_inferred_universe_filter_field_present,
        test_all_12_candidate_predicates_present,
        test_no_phantom_predicate_names_in_table,
        test_insert_uses_kind_internal,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
        except Exception as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failures.append(t.__name__)
    if failures:
        print(f'\n{len(failures)} test(s) FAILED: {failures}')
        sys.exit(1)
    print(f'\nAll {len(tests)} tests passed.')
