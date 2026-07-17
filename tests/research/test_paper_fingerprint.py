"""Phase 2 follow-up #17 — paper-fingerprint dedup tests."""


def test_normalize_title_lowercases_and_strips_articles():
    from src.research.paper_fingerprint import normalize_title
    assert normalize_title("The Cross-Section of Expected Stock Returns") == "cross section of expected stock returns"
    assert normalize_title("A Five-Factor Asset Pricing Model") == "five factor asset pricing model"
    assert normalize_title("An Empirical Test") == "empirical test"


def test_normalize_title_handles_punctuation_and_whitespace():
    from src.research.paper_fingerprint import normalize_title
    assert normalize_title("Risk-Parity Portfolio Construction!") == "risk parity portfolio construction"
    assert normalize_title("  Multiple   Spaces  Inside  ") == "multiple spaces inside"


def test_extract_first_author_lastname_handles_both_formats():
    from src.research.paper_fingerprint import extract_first_author_lastname
    assert extract_first_author_lastname(["Eugene F. Fama"]) == "fama"
    assert extract_first_author_lastname(["Fama, Eugene F."]) == "fama"
    assert extract_first_author_lastname(["Eugene F. Fama", "Kenneth R. French"]) == "fama"
    assert extract_first_author_lastname([]) == ""
    assert extract_first_author_lastname(None) == ""


def test_compute_fingerprint_is_deterministic_and_collision_resistant():
    from src.research.paper_fingerprint import compute_fingerprint
    fp1 = compute_fingerprint("The Cross-Section of Expected Stock Returns", ["Fama, E."], 1992)
    fp2 = compute_fingerprint("the CROSS-section of expected stock returns", ["Eugene F. Fama"], 1992)
    assert fp1 == fp2  # same paper, different surface forms
    assert len(fp1) == 16

    fp3 = compute_fingerprint("A Five-Factor Asset Pricing Model", ["Fama, E.", "French, K."], 2015)
    assert fp1 != fp3


def test_compute_fingerprint_returns_none_for_missing_inputs():
    from src.research.paper_fingerprint import compute_fingerprint
    assert compute_fingerprint("", ["Fama"], 1992) is None
    assert compute_fingerprint("Title", [], 1992) is None
    assert compute_fingerprint("Title", ["Fama"], None) is None
    assert compute_fingerprint("Title", None, 1992) is None


def test_compute_fingerprint_distinguishes_same_title_different_year():
    """A revised paper republished a year later should NOT dedup."""
    from src.research.paper_fingerprint import compute_fingerprint
    fp_2020 = compute_fingerprint("Empirical Anomalies", ["Smith, J."], 2020)
    fp_2021 = compute_fingerprint("Empirical Anomalies", ["Smith, J."], 2021)
    assert fp_2020 != fp_2021
