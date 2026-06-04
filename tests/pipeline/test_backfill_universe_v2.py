from scripts.build_backfill_universe_v2 import select_v2_universe


def test_selects_liquid_tradable_minus_covered():
    rows = [
        ("DELL", True, "active", True),    # covered → excluded
        ("RDDT", True, "active", True),    # liquid, uncovered → included
        ("ILLIQ", True, "active", False),  # not easy_to_borrow → excluded
        ("DEADCO", True, "inactive", True),
        ("NOTRADE", False, "active", True),
    ]
    covered = {"DELL"}
    out = select_v2_universe(rows, covered)
    assert out == ["RDDT"]


def test_share_class_dash_normalization():
    rows = [("BRK.B", True, "active", True)]
    out = select_v2_universe(rows, set())
    assert out == ["BRK-B"]
