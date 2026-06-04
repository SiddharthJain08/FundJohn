from scripts.activate_universe_v2 import build_upsert_params


def test_aux_flags_false_and_notes_tagged():
    params = build_upsert_params(["RDDT"], note="sp7-v2-activation 2026-06-XX")
    assert params == [("RDDT", "{SP500_PLUS}", True, "equity", False, False,
                       3650, "sp7-v2-activation 2026-06-XX")]


def test_multiple_tickers_all_same_shape():
    params = build_upsert_params(["AAPL", "MSFT", "RDDT"], note="test-note")
    assert len(params) == 3
    for ticker, membership, active, category, has_options, has_fundamentals, min_history, note in params:
        assert membership == "{SP500_PLUS}"
        assert active is True
        assert category == "equity"
        assert has_options is False
        assert has_fundamentals is False
        assert min_history == 3650
        assert note == "test-note"
    assert params[0][0] == "AAPL"
    assert params[1][0] == "MSFT"
    assert params[2][0] == "RDDT"


def test_empty_list_returns_empty():
    params = build_upsert_params([], note="no-op")
    assert params == []
