"""Phase 1A — assert PaperHunter's arXiv category surface is the expanded set
described in the Fincept-imports master plan."""
from src.ingestion.arxiv_discovery import CATEGORIES, MAX_RESULTS_DEFAULT


def test_categories_include_qfin_and_ml_and_stats():
    cats = set(CATEGORIES)
    # Original q-fin set must still be present
    for c in ['q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM']:
        assert c in cats, f"q-fin category {c} missing from CATEGORIES"
    # New ML / stats / NLP additions from Fincept arxiv_data.py concept-lift
    for c in ['cs.LG', 'cs.AI', 'cs.CL', 'stat.ML']:
        assert c in cats, f"ML/NLP category {c} missing from CATEGORIES"


def test_categories_have_no_duplicates():
    assert len(CATEGORIES) == len(set(CATEGORIES))


def test_per_category_max_results_default_is_sensible():
    # Cap at 1000/cat to avoid arXiv rate-limit ban; must be >= current 200
    assert 200 <= MAX_RESULTS_DEFAULT <= 1000
