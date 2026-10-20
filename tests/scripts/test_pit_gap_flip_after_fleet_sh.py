"""Static text checks on the flip script (executing it would touch the
production .env and systemctl). `bash -n` is a syntax check only."""
from __future__ import annotations

import subprocess
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'pit_gap_flip_after_fleet.sh'
TEXT = PATH.read_text()


def test_already_applied_requires_all_three_flags():
    lines = [l for l in TEXT.splitlines() if l.startswith('if grep') and '&&' in l]
    assert len(lines) == 1
    for flag in ('OPENCLAW_BT_GAP_FILL=open', 'OPENCLAW_FINANCIALS_PIT=1', 'OPENCLAW_MACRO_PIT=1'):
        assert flag in lines[0]


def test_apply_sets_macro_pit():
    assert "s|^OPENCLAW_MACRO_PIT=.*|OPENCLAW_MACRO_PIT=1|" in TEXT
    assert "printf 'OPENCLAW_MACRO_PIT=1" in TEXT
    assert 'FINANCIALS_PIT|BT_GAP_FILL|MACRO_PIT' in TEXT


def test_kill_switch_text_names_macro_pit():
    applied = next(l for l in TEXT.splitlines() if '[pit-gap-flip] APPLIED' in l)
    post = TEXT[TEXT.index(applied):]
    assert 'OPENCLAW_MACRO_PIT=0' in post


def test_bash_syntax_ok():
    assert subprocess.run(['bash', '-n', str(PATH)]).returncode == 0
