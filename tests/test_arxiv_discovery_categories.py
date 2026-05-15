"""Phase 1A — assert PaperHunter's arXiv category surface is the expanded set
described in the Fincept-imports master plan."""
# NOTE: src/ingestion/__init__.py has a long-standing import error
# (`fetch_polygon_universe` not exported by pipeline.py — pre-dates this branch,
# see beea4cd on main). arxiv_discovery.py is already designed to be loaded
# directly (see its own line 28 comment: "local import to avoid src.ingestion
# package __init__"). Mirror that pattern here so this test exercises the
# constants without tripping the unrelated package-init bug.
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "arxiv_discovery",
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ingestion" / "arxiv_discovery.py",
)
arxiv_discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arxiv_discovery)


def test_categories_include_qfin_and_ml_and_stats():
    cats = set(arxiv_discovery.CATEGORIES)
    # Original q-fin set must still be present
    for c in ['q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM']:
        assert c in cats, f"q-fin category {c} missing from CATEGORIES"
    # New ML / stats / NLP additions from Fincept arxiv_data.py concept-lift
    for c in ['cs.LG', 'cs.AI', 'cs.CL', 'stat.ML']:
        assert c in cats, f"ML/NLP category {c} missing from CATEGORIES"


def test_categories_have_no_duplicates():
    assert len(arxiv_discovery.CATEGORIES) == len(set(arxiv_discovery.CATEGORIES))


def test_per_category_max_results_default_is_sensible():
    # Cap at 1000/cat to avoid arXiv rate-limit ban; must be >= current 200
    assert 200 <= arxiv_discovery.MAX_RESULTS_DEFAULT <= 1000
