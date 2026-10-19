"""config_json['macro_pit'] provenance key (six-repo D1c)."""
from __future__ import annotations

import os
from unittest.mock import patch

from tests.backtest.test_open_book import TestConfigJsonExitHook, _mk_plain_cls


def _cfg(env: dict):
    helper = TestConfigJsonExitHook()
    base = {k: v for k, v in os.environ.items() if k != 'OPENCLAW_MACRO_PIT'}
    base.update(env)
    with patch.dict(os.environ, base, clear=True), \
            patch('strategies.aux_data_loader.load_aux_data', return_value={'options': {}}):
        return helper._config_json_of(_mk_plain_cls())


def test_macro_pit_false_when_unset():
    assert _cfg({})['macro_pit'] is False


def test_macro_pit_true_when_set():
    assert _cfg({'OPENCLAW_MACRO_PIT': '1'})['macro_pit'] is True


def test_macro_pit_key_follows_financials_pit():
    keys = list(_cfg({}))
    assert keys.index('macro_pit') == keys.index('financials_pit') + 1
